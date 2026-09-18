"use strict";
/**
 * Node-based tests for src/mapgen/web/static/app.js.
 *
 * Run with:
 *
 *     node tests/js/test_app.js
 *
 * This project has no npm package, no bundler and no JS test framework by
 * design (see the brief's "no other third-party front-end dependencies"),
 * so this is a small, dependency-free runner built on Node's own vm, fs
 * and assert-by-hand rather than pulling in one. It exists specifically
 * because app.js's own defects had reached a browser unfiltered before:
 * a query-string bug that made every geocode and reverse-geocode call
 * fail with 403 shipped in 895a90d and survived an entire review round
 * because nothing here exercised the real fetch URL app.js actually
 * builds. See the fetch stub below for how that is avoided this time.
 *
 * Deliberately out of scope: map rendering, tile loading, and anything
 * else that needs an actual browser and a human looking at it. Nothing
 * here replaces opening the page.
 */

const vm = require("vm");
const fs = require("fs");
const path = require("path");

const STATIC_DIR = path.join(__dirname, "..", "..", "src", "mapgen", "web", "static");
const APP_JS_PATH = path.join(STATIC_DIR, "app.js");
const INDEX_HTML_PATH = path.join(STATIC_DIR, "index.html");
const SOURCE = fs.readFileSync(APP_JS_PATH, "utf8");

// Every id $() is allowed to find, read from the real, committed markup
// rather than hand-listed here, the same "derive, don't duplicate"
// reasoning as the Python asset-reference test. This is what closes the
// gap a re-review found: a mock getElementById that invents an element
// for any id it is asked for cannot tell a typo'd $("draww") from a real
// $("draw"), so a typo that would throw and blank the whole page in a
// real browser instead ran clean here, 12/12, exit 0.
const INDEX_HTML = fs.readFileSync(INDEX_HTML_PATH, "utf8");

// Task 36, item 5 is a layout constraint as much as a feature: the strip
// under the map must not grow and nothing else may move. Nothing in this
// harness can measure a box, so the stylesheet is read for the one
// structural fact the constraint rests on, that the legend's own padding
// and border moved to the row that now holds both it and the bar rather
// than a second set being added beside them. See cssRule further down for
// how narrowly that is asserted.
const STYLES_CSS = fs.readFileSync(path.join(STATIC_DIR, "styles.css"), "utf8");

// Comments stripped before anything below reads ids or attributes out of
// the markup. index.html is heavily commented, and those comments discuss
// the very elements underneath them, so an id or an <input> quoted inside
// one used to be indistinguishable here from an element that genuinely
// exists. That is the same class of gap as every other one this file
// records: it makes the harness MORE permissive than a browser, which
// parses a comment as a comment and nothing else, and it would have let a
// $("...") call survive against an element that had been deleted and
// merely left described in the prose above where it used to be.
const INDEX_HTML_MARKUP = INDEX_HTML.replace(/<!--[\s\S]*?-->/g, "");

const KNOWN_IDS = new Set(
  Array.from(INDEX_HTML_MARKUP.matchAll(/\bid="([^"]+)"/g), (m) => m[1])
);

// The markup's own attributes for each <input id="...">, read from the
// committed index.html rather than restated here, so an element that a
// browser would treat specially because of what the markup says about it
// (a type="range" with min/max/step, today) is treated the same way by
// makeElement below. Review finding I9: the harness implemented no min,
// max or step behaviour at all, even though applyTileSizeBounds's own
// comment is written about clamping, so neither the clamping the
// function was built for nor the step snapping it was missing could be
// distinguished from doing nothing.
const INPUT_MARKUP = new Map(
  Array.from(INDEX_HTML_MARKUP.matchAll(/<input\b([^>]*)>/g), (match) => {
    const attrs = {};
    for (const attr of match[1].matchAll(/([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g)) {
      attrs[attr[1]] = attr[2] !== undefined ? attr[2] : "";
    }
    return [attrs.id, attrs];
  }).filter(([id]) => id !== undefined)
);

// --- minimal DOM -----------------------------------------------------

// Parses <input .../> tags out of an HTML string into small, live
// pseudo-elements: enough for app.js's own rendered-checklist pattern
// (a container's innerHTML set to a template string of <label><input
// type="checkbox" value="..." checked /><span>...</span></label>
// entries, later read back with document.querySelectorAll) to be
// genuinely exercised in this harness, rather than the container's
// children being permanently invisible to any query. Deliberately not a
// general HTML parser: only <input> tags are recognised, and only the
// attributes app.js's own templates actually use (type, value, checked,
// disabled, and any data-* attribute) are read. A checkbox's `checked`
// is read ONLY from the markup at parse time, matching how app.js
// renders it (server data decides which boxes start ticked); nothing
// here needs to react to a later `.checked = ...` mutation on these
// objects being written back into the source HTML, since app.js itself
// never re-reads a container's innerHTML after rendering it, only
// queries the live objects this returns.
function _parseInputs(html) {
  const inputs = [];
  const tagRe = /<input\b([^>]*)>/gi;
  let match;
  while ((match = tagRe.exec(html))) {
    const attrs = {};
    const attrRe = /([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g;
    let attrMatch;
    while ((attrMatch = attrRe.exec(match[1]))) {
      attrs[attrMatch[1]] = attrMatch[2] !== undefined ? attrMatch[2] : true;
    }
    inputs.push({
      tagName: "input",
      type: attrs.type === true || attrs.type === undefined ? "text" : attrs.type,
      value: attrs.value !== undefined && attrs.value !== true ? attrs.value : "",
      checked: attrs.checked === true || attrs.checked === "checked" || attrs.checked === "true",
      disabled: attrs.disabled === true || attrs.disabled === "true",
      _attrs: attrs,
      getAttribute(name) {
        if (!(name in this._attrs)) return null;
        return this._attrs[name] === true ? "" : this._attrs[name];
      },
      hasAttribute(name) {
        return name in this._attrs;
      },
    });
  }
  return inputs;
}

function makeElement(id) {
  let html = "";
  // Task 28. A <select>'s value is not an ordinary property: a browser
  // accepts an assignment only when one of its own <option>s carries that
  // value, and quietly reports "" otherwise. A plain property here would
  // be more permissive than any browser, and would pass code that
  // assigns the value BEFORE rendering the options, or that assigns a
  // model none of the options carry. Either reaches the owner as a
  // settings panel that looks right and then sends an empty model, which
  // the server refuses with a 400.
  //
  // Only an element that has actually been given <option> markup gets
  // this behaviour, so every plain input here is unaffected, as is the
  // theme select, whose options live in index.html and are never
  // rendered through innerHTML.
  let optionValues = [];
  let value = "";
  // Review finding I9. A <input type="range">'s value is not an ordinary
  // property either, and for a second reason on top of the select's: a
  // browser runs the range state's value sanitization algorithm on every
  // assignment, which CLAMPS to min/max and then SNAPS to the step grid
  // ("when the element is suffering from a step mismatch, the user agent
  // must round the element's value to the nearest number for which the
  // element would not", and where two are equally near, the larger).
  //
  // Without this, applyTileSizeBounds could not be tested at all: its own
  // comment is written about clamping, and a plain property models
  // neither the clamping it was built for nor the snapping it was
  // missing. The two checks that existed used 20000 and 300, both on the
  // 100 m grid, so neither could have told the difference. This is the
  // same reasoning the <select> above records: a plain property here
  // would be more permissive than any browser.
  //
  // Driven off the committed markup, so only an element index.html
  // actually declares as a range gets range semantics, and it picks up
  // the real min/max/step rather than numbers restated here.
  const markup = INPUT_MARKUP.get(id) || {};
  const isRange = markup.type === "range";
  const sanitiseRange = (next) => {
    let number = Number(next);
    const min = Number(element.min);
    const max = Number(element.max);
    if (!Number.isFinite(number)) {
      number = Number.isFinite(min) ? min : 0;
    }
    if (Number.isFinite(min) && number < min) number = min;
    if (Number.isFinite(max) && number > max) number = max;
    const step = Number(element.step);
    if (element.step !== "any" && Number.isFinite(step) && step > 0) {
      const base = Number.isFinite(min) ? min : 0;
      // Math.round breaks a tie upwards, which is what the spec asks for
      // ("if two numbers satisfy all these constraints, user agents must
      // use the one nearest to positive infinity"): 1250 on a grid based
      // at 500 with a step of 100 becomes 1300, not 1200, which is what
      // Chrome and Firefox both do.
      number = base + Math.round((number - base) / step) * step;
      if (Number.isFinite(min) && number < min) number += step;
      if (Number.isFinite(max) && number > max) number -= step;
    }
    return String(number);
  };
  // Attributes set through setAttribute, kept apart from the plain
  // properties above because they are not the same thing: app.js's
  // progress bar (Task 27) writes aria-valuenow here, which no property
  // on a real element mirrors, and a test that could only read
  // textContent would be unable to tell whether a screen reader was
  // being told the same number the bar was drawing.
  const attributes = {};
  const element = {
    id,
    // The markup's own min/max/step to begin with, exactly as a browser
    // starts from them, so a test that never touches these still sees
    // index.html's real numbers rather than nothing at all.
    min: markup.min !== undefined ? markup.min : "",
    max: markup.max !== undefined ? markup.max : "",
    step: markup.step !== undefined ? markup.step : "",
    get value() {
      return value;
    },
    set value(next) {
      if (optionValues.length && !optionValues.includes(String(next))) {
        value = "";
        return;
      }
      if (isRange) {
        value = sanitiseRange(next);
        return;
      }
      value = String(next);
    },
    checked: false,
    disabled: false,
    hidden: false,
    className: "",
    textContent: "",
    style: {},
    // Task 38, item 4. A real element reports its laid-out height here
    // and this harness cannot lay anything out, so it starts at 0 and a
    // test that cares supplies the two numbers the split is worked out
    // from (the map's height and the log's). 0 rather than undefined
    // because that is what a real element not in the document reports,
    // and app.js has to survive being asked before layout either way.
    offsetHeight: 0,
    scrollHeight: 0,
    scrollTop: 0,
    children: [],
    setAttribute(name, value) {
      attributes[name] = String(value);
    },
    getAttribute(name) {
      return name in attributes ? attributes[name] : null;
    },
    removeAttribute(name) {
      delete attributes[name];
    },
    hasAttribute(name) {
      return name in attributes;
    },
    // The live pseudo-inputs parsed from whatever was last assigned to
    // innerHTML, consulted by document.querySelectorAll below. Present
    // on every element, empty for one nothing was ever assigned to.
    _inputs: [],
    get innerHTML() {
      return html;
    },
    set innerHTML(markup) {
      html = markup;
      this._inputs = _parseInputs(markup);
      optionValues = Array.from(
        String(markup).matchAll(/<option\b[^>]*\bvalue="([^"]*)"/g),
        (match) => match[1]
      );
      // Rendering a select's options in a browser sets its value to the
      // first of them, since none of the options app.js writes carries
      // `selected`. Whatever was assigned before is gone: a select with
      // no options ignores an assignment outright, and rendering options
      // afterwards does not bring it back. That is what makes writing
      // the value BEFORE the options a real bug rather than a harmless
      // ordering preference, and it is the reason this line exists
      // rather than a gentler "keep it if it still matches".
      if (optionValues.length) value = optionValues[0];
      // And assigning innerHTML REPLACES an element's children, which
      // this stub used to leave standing. That is not a detail: the log
      // is built with appendChild and cleared with innerHTML = "", so a
      // press of Download that wipes the running job's log looked from
      // here like a press that left it alone, and the mutation which
      // removes the guard against exactly that survived a full run. A
      // stub more forgiving than a browser is the one thing this file
      // exists to not be.
      this.children = [];
    },
    // A real element supports multiple listeners per event type; this
    // stored a single handler per type and let a later addEventListener
    // for the same type silently replace an earlier one, which is not
    // how addEventListener works and is exactly the kind of stub-vs-
    // browser mismatch this harness exists to not repeat. app.js relies
    // on this directly: refreshEstimate and maybePersistFieldSettings are
    // two separate listeners on the same "change" event for output-root,
    // tile-size and overlap, and a single-slot mock silently drops
    // whichever was registered first.
    _listeners: {},
    addEventListener(type, handler) {
      (this._listeners[type] = this._listeners[type] || []).push(handler);
    },
    // eventLike lets a test simulate a keydown's key, or any other event
    // property a handler reads, without pulling in a real Event class:
    // app.js only ever reads a handful of plain properties (key,
    // preventDefault()) off whatever it is handed, never anything that
    // needs a real DOM Event's prototype chain.
    fire(type, eventLike = {}) {
      const event = { preventDefault() {}, ...eventLike };
      for (const handler of this._listeners[type] || []) handler(event);
    },
    // The real DOM method, as opposed to fire() above, which is this
    // harness's own test-only shorthand. app.js calls this directly
    // (Task 28: a picked folder path is put through the same "change"
    // listeners a typed one goes through, since assigning .value never
    // fires one on its own), so it has to exist here as more than a
    // synonym a test happens to know about.
    //
    // Deliberately does NOT bubble, and that limitation is worth naming:
    // a real event dispatched on an element also reaches listeners on
    // its ancestors, which is how the delegated #api-keys listener works
    // in a browser. Nothing in app.js dispatches an event that needs to
    // bubble, and the delegated listener is exercised through
    // setApiKeyField below, which fires on the container directly.
    dispatchEvent(event) {
      for (const handler of this._listeners[event.type] || []) handler(event);
      return true;
    },
    appendChild(node) {
      this.children.push(node);
    },
  };
  return element;
}

// Selectors app.js actually uses against a rendered checklist container:
// "#id", "#id input", "#id input:checked", and an attribute clause
// ("#id input[data-config-field]" or ...[data-config-field="x"]"),
// against the live pseudo-inputs _parseInputs produced. Not a general
// CSS engine: an unsupported selector shape throws rather than quietly
// matching nothing, so a typo'd or unanticipated selector fails the
// test loudly instead of passing 0 elements found.
function _queryContainer(container, selector) {
  const parts = selector.trim().split(/\s+/);
  if (parts.length > 2 || !parts[0].startsWith("#")) {
    throw new Error(`unsupported selector in this test harness: ${selector}`);
  }
  if (parts.length === 1) return [container];
  const rest = parts[1];
  const tagMatch = rest.match(/^[a-zA-Z]+/);
  if (!tagMatch || tagMatch[0] !== "input") {
    throw new Error(`unsupported selector in this test harness: ${selector}`);
  }
  let candidates = container._inputs.slice();
  if (rest.includes(":checked")) {
    candidates = candidates.filter((el) => el.checked === true);
  }
  const attrMatch = rest.match(/\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]/);
  if (attrMatch) {
    const [, attrName, attrValue] = attrMatch;
    candidates = candidates.filter((el) =>
      attrValue !== undefined ? el.getAttribute(attrName) === attrValue : el.hasAttribute(attrName)
    );
  }
  return candidates;
}

// A real <html> element, minimal but genuine: app.js's applyTheme
// (Task 22) sets/removes a data-theme attribute on it rather than on any
// id-addressable element (there is no id="theme-root" in the markup, the
// same as a real page, which addresses <html> structurally, not by id).
// Added because applyTheme needed it, the same reason every other
// primitive in this file exists: without it, choosing a theme in
// Settings would be untestable rather than merely inert.
function makeDocumentElement() {
  const attrs = {};
  return {
    setAttribute(name, value) {
      attrs[name] = String(value);
    },
    removeAttribute(name) {
      delete attrs[name];
    },
    getAttribute(name) {
      return name in attrs ? attrs[name] : null;
    },
    hasAttribute(name) {
      return name in attrs;
    },
  };
}

function makeDocument() {
  const elements = new Map();
  const listeners = {};
  const documentElement = makeDocumentElement();
  return {
    documentElement,
    getElementById(id) {
      // A real getElementById returns null, not a fresh element, for an
      // id nothing in the document defines. Checking against KNOWN_IDS
      // (parsed from the real index.html) rather than auto-vivifying
      // whatever app.js asks for means a typo in a $("...") call is
      // handed a null here exactly as a browser would, and app.js's own
      // top-level `$(id).addEventListener(...)` calls throw on it
      // immediately, which fails every test in this file at once: the
      // same "kills the whole page" severity a real typo has, not a
      // single quiet gap.
      if (!KNOWN_IDS.has(id)) return null;
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement() {
      return makeElement("log-line");
    },
    // Resolves against the SAME elements map getElementById uses, so a
    // container queried before it has ever been fetched by id still
    // finds the one live element rather than a second, disconnected one:
    // querySelectorAll("#sources ...") must see whatever $("sources").
    // innerHTML = ... actually wrote, not a fresh, empty stand-in.
    querySelectorAll(selector) {
      const containerId = selector.trim().split(/\s+/)[0].replace(/^#/, "");
      if (!KNOWN_IDS.has(containerId)) return [];
      return _queryContainer(this.getElementById(containerId), selector);
    },
    querySelector(selector) {
      return this.querySelectorAll(selector)[0] || null;
    },
    // document itself needs addEventListener/fire too: the draw tool's
    // Escape handling is bound here rather than on the map container, so
    // it fires regardless of what currently has focus (see app.js).
    addEventListener(type, handler) {
      (listeners[type] = listeners[type] || []).push(handler);
    },
    fire(type, eventLike = {}) {
      const event = { preventDefault() {}, ...eventLike };
      for (const handler of listeners[type] || []) handler(event);
    },
    // Real documents have this, and app.js's keep-alive reads it to tell
    // "came back to the foreground" from "went away". A stub without it
    // would make the returning-page ping untestable.
    visibilityState: "visible",
  };
}

function makeWindow() {
  const listeners = {};
  // Task 22: app.js's effectiveTheme() reads window.matchMedia to resolve
  // "auto" for whatever it draws itself (the tile grid's colours; the
  // page's own light/dark switching is plain CSS and needs no JS at
  // all). Added because effectiveTheme() needed it, not speculatively:
  // a system dark-mode preference is exactly the kind of thing this
  // harness cannot observe from the real OS, so a test controls it
  // through prefersDark below instead.
  let prefersDark = false;
  return {
    addEventListener(type, handler) {
      (listeners[type] = listeners[type] || []).push(handler);
    },
    fire(type, eventLike = {}) {
      const event = { preventDefault() {}, ...eventLike };
      for (const handler of listeners[type] || []) handler(event);
    },
    matchMedia(query) {
      return {
        matches: query.includes("dark") ? prefersDark : false,
        addEventListener() {},
        removeEventListener() {},
      };
    },
    // Test-only hook: sets what matchMedia("(prefers-color-scheme: dark)")
    // reports from here on, standing in for the OS/browser's own signal.
    _setPrefersDark(value) {
      prefersDark = value;
    },
    // There is deliberately no localStorage here. The log's height was
    // kept in one for as long as mapgen.config.Config had no field for
    // it; it has one now, so the split is saved and restored through
    // /api/config like every other setting and this page talks to no
    // store but the server. A stub for an API app.js does not use would
    // be surface that could only ever make a future mistake pass.
  };
}

function makeNavigator() {
  // sendBeacon rather than fetch is the entire point of the pagehide path:
  // a closing page is not guaranteed to live long enough to finish a normal
  // request. Recording the URLs is what lets a test prove the token really
  // made it onto the beacon, which a bare "was it called" check would miss.
  const beacons = [];
  return {
    beacons,
    sendBeacon(url) {
      beacons.push(String(url));
      return true;
    },
  };
}

function makeLeaflet() {
  const mapListeners = {};
  const rectangles = [];
  const markers = [];
  const tileLayers = [];

  // L.latLng accepts [lat, lng] and {lat, lng} interchangeably, and
  // app.js hands markers the array form while Leaflet hands its own drag
  // events the object form. Normalised here for the same reason: a stub
  // that only understood one of the two would be pickier than the real
  // build and would fail code that works.
  const toLatLng = (value) =>
    Array.isArray(value) ? { lat: value[0], lng: value[1] } : { lat: value.lat, lng: value.lng };

  // Task 36, item 3. What the map currently shows, as the four accessors
  // a real LatLngBounds carries rather than a plain object: app.js reads
  // getWest/getSouth/getEast/getNorth off it, and a bare {west, south,
  // ...} here would let a version of app.js that read the wrong shape
  // pass. Movable through _setBounds below, which is how a test pans or
  // zooms the map without a real one.
  let viewBounds = { west: -3.4, south: 51.3, east: -3.0, north: 51.6 };

  const mapObject = {
    setView() {
      return this;
    },
    getBounds() {
      const current = viewBounds;
      return {
        getWest: () => current.west,
        getSouth: () => current.south,
        getEast: () => current.east,
        getNorth: () => current.north,
      };
    },
    on(type, handler) {
      (mapListeners[type] = mapListeners[type] || []).push(handler);
    },
    // Test-only: fires whatever app.js registered via map.on(type, ...)
    // with a fabricated event (eventLike.latlng, typically). This is
    // exactly the boundary the brief draws: Leaflet's own pixel-to-latlng
    // projection and real pointer handling are not exercised, but once
    // Leaflet has decided a click or mousemove happened somewhere, app.js's
    // own reaction to it is ordinary, unit-testable JavaScript.
    fire(type, eventLike = {}) {
      for (const handler of mapListeners[type] || []) handler(eventLike);
    },
    getContainer: () => ({ style: {} }),
    fitBounds() {},
    // Task 38, item 4. The real method (Map.invalidateSize, 1.9.4), which
    // a map whose container has changed size has to be told about or its
    // tiles stay laid out for the box it used to have. Counted rather
    // than accepted silently: "the divider moved the boxes" and "the
    // divider moved the boxes and the map now knows" are different
    // claims, and only the second one leaves usable tiles.
    _invalidateSizeCalls: 0,
    invalidateSize() {
      this._invalidateSizeCalls += 1;
      return this;
    },
    removeLayer(layer) {
      if (layer) layer.removed = true;
    },
    // Task 36, item 2. Leaflet's Drag handler watches the same mousedown
    // the press-drag-release tool now does, so app.js switches it off
    // while the tool is armed. Modelled as the real thing a test can
    // check, its enabled state, rather than as two no-op methods: a stub
    // that merely accepted the calls could not tell "switched off for the
    // whole gesture" from "never switched off at all", which is the
    // difference between drawing a rectangle and panning the map under
    // the cursor while trying to.
    dragging: {
      _enabled: true,
      enabled() {
        return this._enabled;
      },
      enable() {
        this._enabled = true;
      },
      disable() {
        this._enabled = false;
      },
    },
    // Test-only: the map has been panned or zoomed. Fires the events a
    // real Leaflet fires when that happens, so a test can prove app.js
    // is NOT listening to them rather than merely that it did not
    // happen to be called.
    _setBounds(next) {
      viewBounds = next;
      this.fire("move", {});
      this.fire("moveend", {});
      this.fire("zoomend", {});
    },
  };

  return {
    map: () => mapObject,
    // Recorded, not merely accepted: the basemap's own options are what
    // decide whether OSM's tile servers serve it or refuse it (see the
    // referrer test), so a stub that swallowed them could not tell a
    // compliant layer from a blocked one.
    _tileLayers: tileLayers,
    tileLayer: (url, options) => {
      tileLayers.push({ url, options: options || {} });
      return {
        addTo() {
          return this;
        },
      };
    },
    rectangle: (bounds, options) => {
      const layerListeners = {};
      const handle = {
        bounds,
        options,
        removed: false,
        // Task 31 binds a tooltip to a failed tile and takes it off again
        // when that tile recovers. Modelled as the two facts a test can
        // check and the owner meets: whether this rectangle carries a
        // tooltip at all, and what it says.
        //
        // Faithful to Leaflet in the respect that matters here.
        // bindTooltip REPLACES what was bound before (the real one
        // unbinds an open tooltip first, then rebuilds it) and
        // unbindTooltip leaves the layer carrying nothing rather than an
        // empty one. A stub that appended, or that left the old content
        // behind on an unbind, would let a reason for a problem that has
        // been fixed pass for a live one, which is the exact defect this
        // feature exists to prevent.
        tooltip: null,
        tooltipOptions: null,
        bindTooltip(content, tooltipOptions) {
          handle.tooltip = String(content);
          handle.tooltipOptions = tooltipOptions || {};
          return handle;
        },
        unbindTooltip() {
          handle.tooltip = null;
          handle.tooltipOptions = null;
          return handle;
        },
        // Layer.on, the same shape mapObject.on above already has. Real
        // Leaflet also propagates a click on a vector layer up to the
        // map, which is why app.js guards its own handler on the draw
        // tool's state rather than stopping propagation. A test that
        // needs both fires both: modelling the propagation here would be
        // inventing behaviour rather than recording it, and getting the
        // invention subtly wrong is how a stub starts proving things
        // about itself.
        on(type, handler) {
          (layerListeners[type] = layerListeners[type] || []).push(handler);
          return handle;
        },
        fire(type, eventLike = {}) {
          const event = { preventDefault() {}, ...eventLike };
          for (const listener of layerListeners[type] || []) listener(event);
        },
        setBounds(newBounds) {
          handle.bounds = newBounds;
        },
        // Task 22 paints the tile grid by restyling each rectangle in
        // place. Nothing here had setStyle, because no test before Task
        // 27 ran a job with a real tile_grid behind it: every estimate
        // stub returned no grid at all, so tileRectangles stayed empty
        // and every setStyle call was a loop over nothing. The first
        // test to supply a grid found app.js throwing on the first line
        // of its Download handler, which is exactly the stub-more-
        // permissive-than-the-browser gap this file exists to close.
        setStyle(newOptions) {
          handle.options = { ...handle.options, ...newOptions };
        },
        addTo() {
          return handle;
        },
      };
      rectangles.push(handle);
      return handle;
    },
    // Task 38, item 2. The four corner handles are markers with a DIV
    // icon: no image is ever fetched, which is what lets the vendored
    // Leaflet stay without its marker PNGs.
    divIcon: (options) => ({ options: options || {} }),
    marker: (latlng, options) => {
      const markerListeners = {};
      const marker = {
        options: options || {},
        latlng: toLatLng(latlng),
        removed: false,
        getLatLng() {
          return marker.latlng;
        },
        setLatLng(next) {
          marker.latlng = toLatLng(next);
          return marker;
        },
        on(type, handler) {
          (markerListeners[type] = markerListeners[type] || []).push(handler);
          return marker;
        },
        fire(type, eventLike = {}) {
          for (const handler of markerListeners[type] || []) handler(eventLike);
          return marker;
        },
        listens(type) {
          return Boolean(markerListeners[type] && markerListeners[type].length);
        },
        addTo() {
          return marker;
        },
        // Test-only, and modelled on Leaflet's own dispatch rather than
        // on what would be convenient here, because this is the exact
        // place a stub could hide a real bug.
        //
        // Map._findEventTargets walks up the DOM from the pressed
        // element and stops at the map container, which Map._initEvents
        // registers as a target for the map itself. So a mousedown on a
        // marker fires on the MARKER and then on the MAP, and
        // bubblingMouseEvents does not stop it: that option is only
        // consulted for click, dblclick, mouseover, mouseout and
        // contextmenu (Map._mouseEvents). The only thing that stops the
        // second fire is originalEvent._stopped, which is what
        // L.DomEvent.stopPropagation sets, and which Map._fireDOMEvent's
        // loop checks between targets.
        //
        // Every corner of a rectangle is also a point inside it, so a
        // page that does not stop that press has a resize that is also a
        // move. Firing both here is what makes that failure visible.
        _press() {
          const originalEvent = { _stopped: false, buttons: 1 };
          marker.fire("mousedown", { originalEvent, latlng: marker.latlng });
          if (!originalEvent._stopped) {
            mapObject.fire("mousedown", { originalEvent, latlng: marker.latlng });
          }
          return originalEvent;
        },
        // One whole marker drag, in the order Leaflet produces it:
        // the press, then dragstart on the first move (Draggable fires it
        // there, not on the press), then the marker's own latlng updated
        // BEFORE each drag event with that latlng on the event too, then
        // dragend. The map's mousemove and mouseup deliberately do not
        // fire: Leaflet's Draggable binds those on the document, not on
        // the map container, which is exactly why a marker drag survives
        // a release outside the map.
        _dragTo(...points) {
          marker._press();
          marker.fire("dragstart", { latlng: marker.latlng });
          for (const point of points) {
            marker.latlng = toLatLng(point);
            marker.fire("move", { latlng: marker.latlng });
            marker.fire("drag", { latlng: marker.latlng });
          }
          marker.fire("dragend", { latlng: marker.latlng });
          return marker;
        },
      };
      markers.push(marker);
      return marker;
    },
    // The real function's own second branch: a Leaflet layer event is a
    // plain object with no stopPropagation method of its own, so the real
    // implementation falls through to setting originalEvent._stopped,
    // which is the flag Map._fireDOMEvent reads.
    DomEvent: {
      stopPropagation(event) {
        if (event && event.originalEvent) event.originalEvent._stopped = true;
        return this;
      },
    },
    // Test-only hooks, not part of the real Leaflet API: every rectangle
    // ever created in this sandbox in creation order, and the map object
    // itself, so a test can drive map.fire(...) and inspect what setBBox
    // and the draw tool's preview actually did with it.
    _rectangles: rectangles,
    _markers: markers,
    _mapObject: mapObject,
  };
}

// --- fetch stub ----------------------------------------------------------
//
// Not a substring-matching router: api() passes fetch() a real URL object
// (or, for the pre-fix code exercised by finding 1's regression test, a
// plain string that a real browser would resolve against the document's
// own location). Either way this stub turns it into a genuine
// WHATWG URL/URLSearchParams, the same implementation Node and every
// browser both use, so a route handler reading url.pathname and
// url.searchParams is reading exactly what a real server would have
// received. An uncommitted predecessor of this harness matched routes
// with `url.includes(...)`, which is exactly permissive enough to still
// "match" /api/geocode?q=Barry?token=abc, the malformed URL the shipped
// bug actually produced: that is precisely the class of stub this file
// is written to avoid.

const FETCH_ORIGIN = "http://127.0.0.1:54321";

function makeFetchStub(router) {
  const calls = [];
  const fetchFn = async (input, options = {}) => {
    const url = input instanceof URL ? input : new URL(String(input), FETCH_ORIGIN);
    // The Leaflet mock never touches fetch() at all (tile loading is
    // stubbed away entirely, see makeLeaflet), so every call that ever
    // reaches here comes from app.js's own api() and must be loopback:
    // there is no legitimate case in this harness for a different
    // origin. Asserted on the parsed origin, not the raw string, so a
    // host assembled at runtime (["evil","example","net"].join(".")) is
    // still caught: by the time it is a URL object, the pieces have
    // already been joined, whether they were ever one literal or not.
    if (url.origin !== FETCH_ORIGIN) {
      throw new Error(
        `fetch() called ${url.origin}, not the page's own origin (${FETCH_ORIGIN}). ` +
          `The page must never talk to anything but this origin and the map tile servers ` +
          `(which this harness never routes through fetch() at all).`
      );
    }
    const call = { url, options };
    calls.push(call);
    const response = await router(url, options, call);
    if (!response) {
      throw new Error(`unhandled fetch in test stub: ${url.toString()}`);
    }
    return response;
  };
  return { fetch: fetchFn, calls };
}

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

// --- sandbox -------------------------------------------------------------

const DEFAULT_TOKEN = "test-token-123";
const DEFAULT_CONFIG = {
  output_root: "C:\\Users\\Param\\Surveys",
  tile_size_m: 2000,
  overlap_m: 100,
  last_region: "",
  opentopography_api_key: "",
  // Task 38, item 4. What mapgen.config.Config's own default is, and it
  // means "never chosen": a page that reads this must leave the log at
  // the height the stylesheet gave it. Carried here rather than left out
  // so the ordinary fixture is the shape the real endpoint returns.
  log_height_px: 0,
};
const DEFAULT_SOURCES = [
  { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false, api_key_config_field: null },
];
// A compact but structurally real fixture: one plain leaf group and one
// group with children, which is enough to exercise both branches of
// renderCategories without needing to mirror mapgen.categories' full
// fifteen ids in every test that merely needs boot() to succeed.
const DEFAULT_CATEGORIES = [
  { id: "buildings", label: "Buildings", children: [] },
  {
    id: "roads",
    label: "Roads",
    children: [
      { id: "motorway", label: "Motorway" },
      { id: "footpath", label: "Footpath" },
    ],
  },
];

function bootRoutes(extra) {
  // extra is tried FIRST, not last: several tests need to override one of
  // boot()'s own three fetches specifically (a custom /api/sources list,
  // say, to test a keyed source's rendering) while still getting the
  // other two defaults for free. None of the defaults below are ever
  // needed once a test's own extra route recognises the same path, so
  // trying extra first and falling back to these only when it answers
  // null costs the ordinary case (an extra that only cares about some
  // other endpoint entirely) nothing.
  return async (url, options, call) => {
    // Awaited before the truthiness check, not just called: extra is
    // often an async function, which returns an already-truthy pending
    // Promise object synchronously regardless of what it later resolves
    // to, so checking its return value without awaiting first would
    // always "win" here even when extra genuinely has nothing to say
    // about this path and resolves to null.
    const answer = extra ? await extra(url, options, call) : null;
    if (answer) return answer;
    if (url.pathname === "/api/config" && (!options.method || options.method === "GET")) {
      return jsonResponse(200, DEFAULT_CONFIG);
    }
    if (url.pathname === "/api/sources") {
      return jsonResponse(200, DEFAULT_SOURCES);
    }
    if (url.pathname === "/api/categories") {
      return jsonResponse(200, DEFAULT_CATEGORIES);
    }
    return null;
  };
}

// beforeRun runs against the built sandbox before app.js is evaluated in
// it, which is the only moment the page's own layout can be arranged: the
// heights a browser would have measured have to be there before boot()
// restores a saved split against them. Added for Task 38, item 4.
function buildSandbox({ fetch, token = DEFAULT_TOKEN, beforeRun }) {
  const document = makeDocument();
  const windowObject = makeWindow();
  const navigator = makeNavigator();
  const sandbox = {
    location: { search: `?token=${token}`, origin: FETCH_ORIGIN },
    document,
    window: windowObject,
    navigator,
    L: makeLeaflet(),
    fetch,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    URL,
    URLSearchParams,
    // Enough of the real Event for what app.js does with one: construct
    // it with a type and hand it to dispatchEvent. Nothing here reads a
    // property a real Event would carry beyond .type, so a fuller
    // implementation would be inventing surface nothing uses.
    Event: class Event {
      constructor(type) {
        this.type = String(type);
      }
      preventDefault() {}
    },
    AbortController,
    Date,
    Math,
    Promise,
    encodeURIComponent,
    parseFloat,
    Number,
    Object,
  };
  vm.createContext(sandbox);
  if (beforeRun) beforeRun(sandbox);
  vm.runInContext(SOURCE, sandbox, { filename: "app.js" });
  return sandbox;
}

function flush(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function bootedSandbox(extraRoutes, token, beforeRun) {
  const stub = makeFetchStub(bootRoutes(extraRoutes));
  const sandbox = buildSandbox({ fetch: stub.fetch, token, beforeRun });
  await flush(10); // let boot()'s three sequential awaits (config, sources, categories) settle
  return { sandbox, fetchCalls: stub.calls };
}

function setField(sandbox, id, value) {
  const el = sandbox.document.getElementById(id);
  el.value = value;
  el.fire("change");
}

// The place field is a typeahead as of Task 18: app.js listens on "input"
// (fires per keystroke) rather than "change" (fires on blur/commit) so
// suggestions narrow as you type. Every other field still uses setField
// above unchanged, since app.js still binds those to "change".
function typeIntoPlace(sandbox, value) {
  const el = sandbox.document.getElementById("place");
  el.value = value;
  el.fire("input");
}

// API key fields (Task 19) are rendered without their own id, one per
// keyed source, and app.js listens on the #api-keys CONTAINER rather
// than on each field individually (see app.js's own comment on that
// listener). setField's plain el.fire("change") only invokes listeners
// registered on that exact element, so it cannot exercise a delegated
// one: this fires "change" on the container instead, with the real
// input as event.target, which is what a browser does when a change
// event bubbles from a field up to a delegated ancestor listener.
function setApiKeyField(sandbox, configField, value) {
  const input = sandbox.document.querySelector(`#api-keys input[data-config-field="${configField}"]`);
  if (!input) {
    throw new Error(`no rendered api key field for data-config-field="${configField}"`);
  }
  input.value = value;
  sandbox.document.getElementById("api-keys").fire("change", { target: input });
}

// --- tiny test runner ------------------------------------------------

let failures = 0;
let passed = 0;
const failedNames = [];

// A hung test is a FAILING test, not a stalled run.
//
// Two of the extent locks are protected by nothing else. Mutating either of
// them lets a second job start, this file's own status stub runs out of
// distinct replies to hand out, and an await inside the test never settles.
// Without a deadline the whole run simply stopped producing output partway
// through and never exited: no FAIL line, no summary, no non-zero exit. The
// suite was not green, which is the important half, but "the run stalled" is
// read as "this is slow today" long before it is read as "a lock is broken".
//
// Fifteen seconds against a slowest real test well under one; the longest
// wait anywhere in this file is a 900 ms flush. It cannot fire on a slow
// machine without something being genuinely stuck.
//
// The abandoned work is not cancellable from here and is simply left. Every
// test builds its own sandbox, so it has nothing of the next test's to
// disturb, and process.exit at the bottom ends the process whatever is
// still pending.
const TEST_TIMEOUT_MS = 15000;

async function test(name, fn) {
  let timer = null;
  const deadline = new Promise((_resolve, reject) => {
    timer = setTimeout(
      () => reject(new Error(`timed out after ${TEST_TIMEOUT_MS} ms without settling`)),
      TEST_TIMEOUT_MS
    );
  });
  try {
    await Promise.race([fn(), deadline]);
    passed += 1;
    console.log(`PASS  ${name}`);
  } catch (error) {
    failures += 1;
    failedNames.push(name);
    console.log(`FAIL  ${name}`);
    console.log(`      ${error.stack ? error.stack.split("\n").slice(0, 2).join("\n      ") : error}`);
  } finally {
    clearTimeout(timer);
  }
}

function ok(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

(async () => {
  // =======================================================================
  // The basemap must not be refused by OSM's tile servers
  // =======================================================================
  //
  // The OSMF tile usage policy: "Do not set a restrictive Referrer-Policy
  // that prevents the Referer header being sent on requests to
  // tile.openstreetmap.org." index.html sets no-referrer page-wide so the
  // launch token in this page's URL never leaves the machine, and OSM
  // began answering those referer-less tile requests with an "access
  // blocked" image instead of the map. The layer's own referrerPolicy
  // overrides the page's for its tile images only, and strict-origin
  // sends http://127.0.0.1:<port>/ alone: no path, no query, no token.

  await test("the basemap sends an origin-only Referer, so OSM serves the tiles", async () => {
    const { sandbox } = await bootedSandbox();
    const layers = sandbox.L._tileLayers;
    ok(layers.length === 1, `expected exactly one tile layer, got ${layers.length}`);
    ok(
      new URL(layers[0].url.replace(/\{[a-z]\}/g, "0")).host === "tile.openstreetmap.org",
      `expected the OSM tile host, got ${layers[0].url}`
    );
    ok(
      layers[0].options.referrerPolicy === "strict-origin",
      `expected referrerPolicy "strict-origin", got ${JSON.stringify(layers[0].options.referrerPolicy)}`
    );
  });

  await test("the page itself still sends no Referer anywhere else", async () => {
    // The per-layer policy is an exception to this rule, not a
    // replacement for it: everything that is not a tile image keeps it.
    ok(
      /<meta name="referrer" content="no-referrer"\s*\/?>/.test(INDEX_HTML_MARKUP),
      "expected index.html to keep its page-wide no-referrer meta tag"
    );
  });

  await test("the basemap attribution links to the OSM copyright page", async () => {
    // The tile policy and the ODbL both ask for "(c) OpenStreetMap
    // contributors" with OpenStreetMap linking to the copyright page.
    const { sandbox } = await bootedSandbox();
    const attribution = sandbox.L._tileLayers[0].options.attribution || "";
    ok(
      attribution.includes('href="https://www.openstreetmap.org/copyright"'),
      `expected a link to https://www.openstreetmap.org/copyright, got ${attribution}`
    );
    ok(/OpenStreetMap<\/a> contributors/.test(attribution), `unexpected wording: ${attribution}`);
  });

  // =======================================================================
  // Finding 1: the token must survive alongside a route's own query string
  // =======================================================================

  await test(
    "api() adds the token when the path has none of its own (e.g. /api/config)",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
          return jsonResponse(200, DEFAULT_CONFIG);
        }
        return null;
      });
      fetchCalls.length = 0;
      await sandbox.persistConfig({ tile_size_m: 9999 });
      const putCall = fetchCalls.find((c) => (c.options.method || "").toUpperCase() === "PUT");
      ok(putCall, "expected a PUT /api/config call");
      ok(putCall.url.pathname === "/api/config");
      ok(putCall.url.searchParams.get("token") === DEFAULT_TOKEN);
    }
  );

  await test(
    "api() adds the token alongside an existing query parameter without corrupting either",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/geocode") {
          return jsonResponse(200, [
            { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
          ]);
        }
        return null;
      });
      fetchCalls.length = 0;
      await sandbox.runPlaceSearch("Barry, Wales");
      const call = fetchCalls.find((c) => c.url.pathname === "/api/geocode");
      ok(call, "expected a /api/geocode call");
      ok(
        (call.url.search.match(/\?/g) || []).length === 1,
        `expected exactly one "?", got ${call.url.toString()}`
      );
      ok(
        call.url.searchParams.get("token") === DEFAULT_TOKEN,
        `token was ${call.url.searchParams.get("token")}: a null here means it was swallowed ` +
          `into another parameter's value, the original bug`
      );
      ok(
        call.url.searchParams.get("q") === "Barry, Wales",
        `q was ${JSON.stringify(call.url.searchParams.get("q"))}`
      );
    }
  );

  await test(
    "api() adds the token alongside two existing query parameters (/api/reverse)",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/reverse") {
          return jsonResponse(200, { region: "Vale of Glamorgan", site: "Barry" });
        }
        return null;
      });
      fetchCalls.length = 0;
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(500); // past the suggest debounce
      const call = fetchCalls.find((c) => c.url.pathname === "/api/reverse");
      ok(call, "expected a /api/reverse call");
      ok(call.url.searchParams.get("token") === DEFAULT_TOKEN);
      // "!== null" alone passes on corrupted data too: the original bug's
      // failure mode was lon coming back as the non-null string
      // "-3.285?token=abc", the token glued onto it rather than missing.
      // A clean-decimal-number check, plus the actual expected value
      // (the bbox's centre), catches that a null check cannot.
      const lat = call.url.searchParams.get("lat");
      const lon = call.url.searchParams.get("lon");
      const CLEAN_DECIMAL = /^-?\d+(\.\d+)?$/;
      ok(lat !== null && CLEAN_DECIMAL.test(lat), `lat was ${JSON.stringify(lat)}, expected a clean decimal`);
      ok(lon !== null && CLEAN_DECIMAL.test(lon), `lon was ${JSON.stringify(lon)}, expected a clean decimal`);
      ok(Math.abs(parseFloat(lat) - 51.385) < 1e-9, `lat was ${lat}, expected the bbox centre 51.385`);
      ok(Math.abs(parseFloat(lon) - -3.285) < 1e-9, `lon was ${lon}, expected the bbox centre -3.285`);
    }
  );

  // =======================================================================
  // Debounce
  // =======================================================================

  await test(
    "place search is debounced: a rapid burst of edits fires one request for the final value",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/geocode") {
          return jsonResponse(200, [
            { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
          ]);
        }
        return null;
      });
      fetchCalls.length = 0;
      // "input" fires per keystroke, matching a real typeahead: this is
      // the whole reason Task 18 moved off "change", which only fires on
      // blur/commit and could never have narrowed as you type at all.
      typeIntoPlace(sandbox, "Bar");
      await flush(50);
      typeIntoPlace(sandbox, "Barr");
      await flush(50);
      typeIntoPlace(sandbox, "Barry");
      await flush(600); // past PLACE_DEBOUNCE_MS (400ms)
      const geocodeCalls = fetchCalls.filter((c) => c.url.pathname === "/api/geocode");
      ok(geocodeCalls.length === 1, `expected exactly 1 call, got ${geocodeCalls.length}`);
      ok(geocodeCalls[0].url.searchParams.get("q") === "Barry");
    }
  );

  await test(
    "region/site auto-suggest is debounced: a rapid burst of extent changes fires one lookup",
    async () => {
      // The response never resolves, deliberately: an instantly-resolving
      // one first filled region/site from the *first* call, and then
      // suggestNames()'s own "don't overwrite already-filled fields"
      // guard was what kept the second call from firing, a fully correct
      // but unrelated behaviour that passed with the debounce deleted too
      // (confirmed directly). Keeping the response pending means region
      // and site stay empty for the whole test, so a second, undebounced
      // call has nothing stopping it except the debounce, and counting
      // actual fetch() invocations is really counting debounced calls.
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/reverse") return new Promise(() => {});
        return null;
      });
      fetchCalls.length = 0;
      setField(sandbox, "bbox", "-3.30,51.30,-3.20,51.40");
      await flush(50);
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(600);
      const reverseCalls = fetchCalls.filter((c) => c.url.pathname === "/api/reverse");
      ok(reverseCalls.length === 1, `expected exactly 1 call, got ${reverseCalls.length}`);
    }
  );

  // =======================================================================
  // Abort path
  // =======================================================================

  await test("runPlaceSearch aborts its own previous in-flight request", async () => {
    let firstSignal = null;
    let callCount = 0;
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/geocode") {
        callCount += 1;
        if (callCount === 1) {
          firstSignal = options.signal;
          return new Promise(() => {}); // never resolves on its own
        }
        return jsonResponse(200, [
          { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
        ]);
      }
      return null;
    });
    sandbox.runPlaceSearch("first"); // fire and forget; hangs
    await flush(10);
    ok(firstSignal !== null, "expected the first call to have started");
    ok(firstSignal.aborted === false, "first signal should not be aborted yet");
    sandbox.runPlaceSearch("second");
    await flush(10);
    ok(firstSignal.aborted === true, "starting a second search should abort the first");
  });

  await test("suggestNames aborts its own previous in-flight request", async () => {
    let firstSignal = null;
    let callCount = 0;
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/reverse") {
        callCount += 1;
        if (callCount === 1) {
          firstSignal = options.signal;
          return new Promise(() => {}); // never resolves; keeps region/site empty
        }
        return jsonResponse(200, { region: "R", site: "S" });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.30,51.30,-3.20,51.40");
    await flush(500); // past the debounce; first reverse call now hanging
    ok(firstSignal !== null, "expected the first lookup to have started");
    ok(firstSignal.aborted === false);
    sandbox.suggestNames(); // direct call, bypassing the debounce, to isolate the abort
    await flush(10);
    ok(firstSignal.aborted === true, "starting a second lookup should abort the first");
  });

  // =======================================================================
  // Conditional persistence, including the regression on the ordinary
  // (no extent drawn yet) case
  // =======================================================================

  await test(
    "output-root persists immediately when there is not yet an extent to validate it against (regression guard)",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox();
      fetchCalls.length = 0;
      setField(sandbox, "output-root", "D:\\NewSurveys");
      await flush(10);
      const putCalls = fetchCalls.filter((c) => (c.options.method || "").toUpperCase() === "PUT");
      ok(putCalls.length === 1, `expected one immediate PUT, got ${putCalls.length}`);
      const body = JSON.parse(putCalls[0].options.body);
      ok(body.output_root === "D:\\NewSurveys", `body was ${putCalls[0].options.body}`);
    }
  );

  await test(
    "output-root persists only once a real estimate succeeds, once there is one to run",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/estimate") {
          return jsonResponse(200, {
            tiles: 1,
            rows: 1,
            cols: 1,
            extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000,
            seconds_estimate: 60,
            warnings: [],
            folder: "C:\\Users\\Param\\Surveys\\South-Wales\\2026-08-02_Barry",
          });
        }
        if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
          return jsonResponse(200, DEFAULT_CONFIG);
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(500); // let the suggest debounce clear so it can't interfere
      setField(sandbox, "region", "South Wales");
      setField(sandbox, "site", "Barry");
      await flush(10);
      fetchCalls.length = 0;
      setField(sandbox, "output-root", "D:\\NewSurveys");
      await flush(10);
      const putCalls = fetchCalls.filter((c) => (c.options.method || "").toUpperCase() === "PUT");
      ok(putCalls.length === 1, `expected exactly one PUT once the estimate succeeded, got ${putCalls.length}`);
    }
  );

  await test(
    "output-root is NOT persisted when the estimate rejects it (the original finding)",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/estimate") {
          return jsonResponse(400, { error: "path too long" });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(500);
      setField(sandbox, "region", "South Wales");
      setField(sandbox, "site", "Barry");
      await flush(10);
      fetchCalls.length = 0;
      setField(sandbox, "output-root", "C:\\" + "x".repeat(250));
      await flush(10);
      const putCalls = fetchCalls.filter((c) => (c.options.method || "").toUpperCase() === "PUT");
      ok(putCalls.length === 0, `expected no PUT when the estimate rejects, got ${putCalls.length}`);
    }
  );

  // =======================================================================
  // Task 19, item 4: the page's own half of "closing the interface stops
  // the server": a periodic heartbeat ping, and a visible Stop server
  // button. The server-side watchdog and /api/heartbeat/api/shutdown
  // routes are tested directly in test_web_server.py; this is only
  // whether the PAGE calls them the way that server expects.
  // =======================================================================

  // app.js's own HEARTBEAT_INTERVAL_MS is declared with const, which,
  // unlike a function declaration, does NOT become a property of the vm
  // sandbox's global object: sandbox.HEARTBEAT_INTERVAL_MS is undefined,
  // not 5000. Duplicated here rather than read off the sandbox, the same
  // way other debounce durations elsewhere in this file (400ms, 600ms)
  // are already asserted against as plain literals rather than exported
  // constants.
  const EXPECTED_HEARTBEAT_INTERVAL_MS = 5000;

  await test(
    "the page pings /api/heartbeat on its own interval, unprompted by any action",
    async () => {
      // Real time, not simulated: this harness has no fake-timer support,
      // so proving the interval genuinely fires means genuinely waiting
      // slightly past it, the same trade-off already accepted elsewhere
      // in this file for the job poller's 700ms tick.
      const { fetchCalls, sandbox } = await bootedSandbox();
      fetchCalls.length = 0;
      await flush(EXPECTED_HEARTBEAT_INTERVAL_MS + 300);
      const heartbeats = fetchCalls.filter((c) => c.url.pathname === "/api/heartbeat");
      ok(heartbeats.length >= 1, `expected at least one heartbeat ping, got ${heartbeats.length}`);
      ok((heartbeats[0].options.method || "").toUpperCase() === "POST");
    }
  );

  // The owner lost a working session to exactly this: they switched tabs
  // to register for an API key, the browser throttled the hidden page's
  // timers below the ping interval, and the server treated the silence as
  // a closed page and shut down. Silence alone cannot tell a backgrounded
  // tab from a closed one, so these three pin the two signals that can.

  await test("returning to the foreground pings immediately, without waiting for the interval", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox();
    fetchCalls.length = 0;
    sandbox.document.visibilityState = "visible";
    sandbox.document.fire("visibilitychange");
    await flush(10);
    const pings = fetchCalls.filter((c) => c.url.pathname === "/api/heartbeat");
    ok(
      pings.length >= 1,
      "a page coming back to the foreground must ping at once, not wait up to a full interval"
    );
  });

  await test("going to the background does not ping", async () => {
    // The mirror of the above: visibilitychange fires in both directions,
    // and only the returning edge should ping. A handler that pinged on
    // every change would pass the test above while being wrong.
    const { fetchCalls, sandbox } = await bootedSandbox();
    fetchCalls.length = 0;
    sandbox.document.visibilityState = "hidden";
    sandbox.document.fire("visibilitychange");
    await flush(10);
    const pings = fetchCalls.filter((c) => c.url.pathname === "/api/heartbeat");
    ok(pings.length === 0, `expected no ping when hidden, got ${pings.length}`);
  });

  await test("a genuine close reports itself by beacon, with the token attached", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.window.fire("pagehide");
    const beacons = sandbox.navigator.beacons;
    ok(beacons.length === 1, `expected exactly one beacon, got ${beacons.length}`);
    const sent = new URL(beacons[0]);
    ok(sent.pathname === "/api/closing", `beacon went to ${sent.pathname}`);
    ok(
      sent.searchParams.get("token") === DEFAULT_TOKEN,
      "the beacon must carry the token or the server will reject it as unauthorised"
    );
  });

  await test("an unreachable server is reported as stopped, not as a raw browser error", async () => {
    // fetch() rejects with a bare "Failed to fetch" when it cannot reach
    // the server, which the owner saw as a red box that looked like the
    // tool was broken rather than like the server had stopped.
    const { sandbox } = await bootedSandbox(() => {
      throw new TypeError("Failed to fetch");
    });
    let caught = null;
    try {
      await sandbox.api("/api/config");
    } catch (error) {
      caught = error;
    }
    ok(caught !== null, "expected the unreachable server to surface as an error");
    ok(caught.serverGone === true, "expected the error to be flagged as the server being gone");
    ok(
      !/failed to fetch/i.test(caught.message),
      `the browser's own wording must not reach the owner: ${caught.message}`
    );
    ok(
      /start mapgen again/i.test(caught.message),
      `the message must say what actually helps: ${caught.message}`
    );
  });

  await test("a single failed heartbeat ping is swallowed quietly, not thrown as unhandled", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/heartbeat") return jsonResponse(500, { error: "boom" });
      return null;
    });
    // What this actually proves: app.js's heartbeat call ends in
    // .catch(() => {}), so a failing ping's rejection is handled, not
    // left unhandled. It does not fail cleanly through ok() if that
    // .catch were removed; a genuinely unhandled rejection is a runtime
    // event Node reports on its own (and, by default, exits non-zero
    // for), which would surface as this whole test FILE crashing rather
    // than one named check failing. Kept anyway: that crash is still a
    // real, visible, CI-breaking signal, just a blunter one than ok().
    await flush(EXPECTED_HEARTBEAT_INTERVAL_MS + 300);
    ok(true, "expected the failed ping to be caught, not thrown");
  });

  await test("the Stop server button calls /api/shutdown and logs a confirmation", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/shutdown") return jsonResponse(200, { stopping: true });
      return null;
    });
    sandbox.document.getElementById("stop-server").fire("click");
    await flush(10);
    const call = fetchCalls.find((c) => c.url.pathname === "/api/shutdown");
    ok(call, "expected a call to /api/shutdown");
    ok((call.options.method || "").toUpperCase() === "POST");
    const logLines = sandbox.document.getElementById("log").children;
    ok(
      logLines.some((l) => /stopped/i.test(l.textContent)),
      `expected a confirmation log line, got: ${logLines.map((l) => l.textContent).join(" | ")}`
    );
  });

  await test("a failed Stop server request logs a clean failure rather than throwing", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/shutdown") return jsonResponse(500, { error: "boom" });
      return null;
    });
    sandbox.document.getElementById("stop-server").fire("click");
    await flush(10);
    const logLines = sandbox.document.getElementById("log").children;
    const failLine = logLines.find((l) => l.className === "fail");
    ok(failLine, "expected a fail-styled log line");
    ok(/could not stop/i.test(failLine.textContent), `unexpected message: ${failLine.textContent}`);
  });

  // =======================================================================
  // boot() and the job poller must fail visibly, not silently
  // =======================================================================

  await test("boot() reports a clean failure instead of leaving the page blank", async () => {
    const stub = makeFetchStub(async (url) => {
      if (url.pathname === "/api/config") {
        return jsonResponse(403, { error: "Invalid or missing token." });
      }
      return null;
    });
    const sandbox = buildSandbox({ fetch: stub.fetch });
    await flush(10);
    const logLines = sandbox.document.getElementById("log").children;
    const failLine = logLines.find((l) => l.className === "fail");
    ok(failLine, `expected a fail-styled log line; log had ${logLines.length} line(s)`);
    ok(
      /token/i.test(failLine.textContent) || /could not load/i.test(failLine.textContent),
      `unexpected message: ${failLine.textContent}`
    );
  });

  await test(
    "the job poller reports a clean failure instead of leaving download/cancel stuck forever",
    async () => {
      let pollCount = 0;
      const { sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/jobs" && (options.method || "").toUpperCase() === "POST") {
          return jsonResponse(202, { id: "job1" });
        }
        if (url.pathname === "/api/jobs/job1") {
          pollCount += 1;
          return jsonResponse(403, { error: "Invalid or missing token." });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "South Wales");
      setField(sandbox, "site", "Barry");
      sandbox.document.getElementById("download").fire("click");
      await flush(50);
      ok(sandbox.document.getElementById("cancel").hidden === false, "expected cancel showing once started");
      ok(sandbox.document.getElementById("download").disabled === true, "expected download disabled once started");
      await flush(900); // past one 700ms poll interval, which fails
      const pollCountAfterFailure = pollCount;
      ok(pollCountAfterFailure >= 1, "expected at least one poll attempt");
      ok(sandbox.document.getElementById("cancel").hidden === true, "expected cancel to hide after the poll failed");
      ok(
        sandbox.document.getElementById("download").disabled === false,
        "expected download to re-enable after the poll failed"
      );
      const logLines = sandbox.document.getElementById("log").children;
      const failLine = logLines.find((l) => l.className === "fail" && /lost contact/i.test(l.textContent));
      ok(failLine, `expected a "lost contact" log line; got: ${logLines.map((l) => l.textContent).join(" | ")}`);
      // pollCount >= 1 alone is satisfied equally by "polled once, then
      // stopped" and "polls forever": exactly the leak the comment next
      // to clearInterval(poller) in app.js claims to prevent, and exactly
      // what removing that one call would not have been caught by above.
      // Waiting for another full interval and requiring the count to be
      // unchanged is what actually proves the interval stopped.
      await flush(800); // long enough for another 700ms tick, if not stopped
      ok(
        pollCount === pollCountAfterFailure,
        `expected no further polls after the failure; had ${pollCountAfterFailure}, now ${pollCount}`
      );
    }
  );

  // =======================================================================
  // Task 18, item 1: the missing-field message names the field or fields
  // actually missing, not a fixed message regardless of which are empty.
  // This is the owner's own reported bug: region filled by the reverse
  // lookup, site genuinely empty, message said "region and site" either way.
  // =======================================================================

  await test(
    "the missing-field message names only the site when the region is already filled",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 4, rows: 2, cols: 2, extent_km: { width: 1.2, height: 0.8 } });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "Vale of Glamorgan");
      await flush(10);
      const message = sandbox.document.getElementById("estimate").innerHTML;
      ok(/\bsite\b/i.test(message), `expected the message to name "site", got: ${message}`);
      ok(!/\bregion\b/i.test(message), `expected "region" not to be named (it is filled), got: ${message}`);
      ok(sandbox.document.getElementById("download").disabled === true);
    }
  );

  await test(
    "the missing-field message names only the region when the site is already filled",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 0.5, height: 0.5 } });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "site", "Barry Waterfront");
      await flush(10);
      const message = sandbox.document.getElementById("estimate").innerHTML;
      ok(/\bregion\b/i.test(message), `expected the message to name "region", got: ${message}`);
      ok(!/\bsite\b/i.test(message), `expected "site" not to be named (it is filled), got: ${message}`);
    }
  );

  await test(
    "the missing-field message names both region and site when both are missing",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      const message = sandbox.document.getElementById("estimate").innerHTML;
      ok(
        /\bregion\b/i.test(message) && /\bsite\b/i.test(message),
        `expected both region and site named, got: ${message}`
      );
    }
  );

  await test(
    "the missing-field message asks to draw or paste an extent when none exists yet",
    async () => {
      const { sandbox } = await bootedSandbox();
      setField(sandbox, "region", "South Wales");
      await flush(10);
      const message = sandbox.document.getElementById("estimate").textContent;
      ok(/extent/i.test(message), `expected the message to mention the extent, got: ${message}`);
      ok(sandbox.document.getElementById("download").disabled === true);
      ok(sandbox.document.getElementById("folder-preview").hidden === true);
    }
  );

  // =======================================================================
  // Task 36, item 1: the Download gate re-checks itself once the reverse
  // lookup has filled in the names it was waiting for.
  //
  // The reported failure exists only on the boot path, so neither test
  // below ever touches region or site: a region restored from config and
  // a site filled in by /api/reverse are both assigned from script, and
  // assigning .value fires no change event, so nothing asked the one gate
  // (refreshEstimate's own success path) to look again. Every field on
  // screen read as filled and Download sat dead. A test that sets those
  // fields with setField, which fires a real change event, exercises the
  // route that always worked and proves nothing about this one.
  // =======================================================================

  function namedBootRoutes({ reverse, allowEstimate = () => true }) {
    return (url) => {
      if (url.pathname === "/api/config") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, last_region: "Bristol" });
      }
      if (url.pathname === "/api/reverse") return jsonResponse(200, reverse);
      if (url.pathname === "/api/extent") {
        return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
      }
      if (url.pathname === "/api/estimate") {
        // Unanswered when the guard is meant to have stopped the request:
        // reaching this route with nothing selected is the guard failing,
        // and an unhandled-fetch error is the right way for that to fail.
        if (!allowEstimate()) return null;
        return jsonResponse(200, {
          tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000, seconds_estimate: 60, warnings: [], folder: "C:\\out",
        });
      }
      return null;
    };
  }

  await test(
    "Download comes alive when the reverse lookup fills the names, with neither name touched",
    async () => {
      const { sandbox } = await bootedSandbox(
        namedBootRoutes({ reverse: { region: "Bristol", site: "Lawrence Weston" } })
      );
      // Restored from config at boot and never touched, exactly as the
      // owner meets it at the start of a session.
      ok(
        sandbox.document.getElementById("region").value === "Bristol",
        "expected the saved region restored by boot()"
      );
      ok(
        sandbox.document.getElementById("output-root").value === DEFAULT_CONFIG.output_root,
        "expected the saved output root restored by boot()"
      );

      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      ok(
        sandbox.document.getElementById("download").disabled === true,
        "expected Download still disabled while the site is genuinely empty"
      );

      // setBBox's own debounced suggestNames, and then the estimate it
      // now asks for.
      await flush(600);
      ok(
        sandbox.document.getElementById("site").value === "Lawrence Weston",
        "expected the reverse lookup to have filled the site"
      );
      ok(
        sandbox.document.getElementById("download").disabled === false,
        "expected Download enabled once every field it needs is filled, without touching one"
      );
      const shown = sandbox.document.getElementById("estimate").innerHTML;
      ok(
        !/enter a/i.test(shown),
        `expected the estimate to stop asking for names it now has, got: ${shown}`
      );
    }
  );

  await test(
    "a lookup that fills nothing asks for no fresh estimate at all",
    async () => {
      // The other half of the same rule: refreshEstimate is asked to look
      // again because its inputs CHANGED, not on every reverse lookup.
      // A lookup that comes back with nothing usable leaves the page
      // exactly as it was and costs no second /api/extent request.
      const { sandbox, fetchCalls } = await bootedSandbox(
        namedBootRoutes({ reverse: { region: "", site: "" } })
      );
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      const extentCallsBefore = fetchCalls.filter((c) => c.url.pathname === "/api/extent").length;
      await flush(600);
      ok(
        fetchCalls.some((c) => c.url.pathname === "/api/reverse"),
        "expected the reverse lookup to have run"
      );
      ok(
        fetchCalls.filter((c) => c.url.pathname === "/api/extent").length === extentCallsBefore,
        "expected no second extent request for a lookup that filled nothing"
      );
      ok(
        sandbox.document.getElementById("download").disabled === true,
        "expected Download still disabled with the site still empty"
      );
    }
  );

  await test(
    "an empty category selection still disables Download after the lookup fills the names",
    async () => {
      // The rule Task 21 added must survive item 1: this is the case a
      // second, separate gate would have broken, since the names are all
      // present and only the category selection is empty.
      let allowEstimate = true;
      const { sandbox } = await bootedSandbox(
        namedBootRoutes({
          reverse: { region: "Bristol", site: "Lawrence Weston" },
          allowEstimate: () => allowEstimate,
        })
      );
      allowEstimate = false;
      sandbox.document.querySelectorAll("#categories input").forEach((box) => {
        box.checked = false;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(600);
      ok(
        sandbox.document.getElementById("site").value === "Lawrence Weston",
        "expected the reverse lookup to have filled the site"
      );
      ok(
        sandbox.document.getElementById("download").disabled === true,
        "expected an empty category selection to keep Download disabled"
      );
      const message = sandbox.document.getElementById("estimate").innerHTML;
      ok(/categor/i.test(message), `expected the reason to mention categories, got: ${message}`);
    }
  );

  // =======================================================================
  // Task 18, item 6: live extent feedback from /api/extent, needing no
  // region or site, reusing the server's tiling maths rather than any
  // client-side reimplementation of it.
  // =======================================================================

  await test(
    "drawing an extent with no names yet shows live geometry from /api/extent",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 4, rows: 2, cols: 2, extent_km: { width: 1.2, height: 0.8 } });
        }
        return null;
      });
      fetchCalls.length = 0;
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      const call = fetchCalls.find((c) => c.url.pathname === "/api/extent");
      ok(call, "expected a POST /api/extent call");
      ok((call.options.method || "").toUpperCase() === "POST");
      const body = JSON.parse(call.options.body);
      ok(body.bbox === "-3.29,51.38,-3.28,51.39", `unexpected bbox sent: ${body.bbox}`);
      ok(
        typeof body.tile_size_m === "number" && typeof body.overlap_m === "number",
        `expected numeric tile_size_m/overlap_m, got: ${call.options.body}`
      );

      const html = sandbox.document.getElementById("estimate").innerHTML;
      ok(html.includes("0.96"), `expected the computed area (1.2 x 0.8 km2), got: ${html}`);
      ok(html.includes("4"), `expected the tile count, got: ${html}`);
    }
  );

  await test(
    "the extent preview re-fetches with an updated tile size while names are incomplete",
    async () => {
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      fetchCalls.length = 0;
      setField(sandbox, "tile-size", "500");
      await flush(10);
      const call = fetchCalls.find((c) => c.url.pathname === "/api/extent");
      ok(call, "expected a fresh /api/extent call after changing tile size");
      const body = JSON.parse(call.options.body);
      ok(body.tile_size_m === 500, `expected the updated tile size in the request, got ${body.tile_size_m}`);
    }
  );

  await test(
    "a failed /api/extent shows its own error alongside the missing-names message, not swallowed",
    async () => {
      // Review round 1: this used to render ONLY the missing-fields line,
      // discarding whatever /api/extent actually said (an absurd-tiling
      // rejection, a zero-area box, a network hiccup), so a genuine
      // problem with the drawn extent was invisible until both names
      // were typed and a full /api/estimate finally surfaced it.
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") return jsonResponse(500, { error: "boom" });
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      const html = sandbox.document.getElementById("estimate").innerHTML;
      ok(/boom/i.test(html), `expected the /api/extent error itself to be shown, got: ${html}`);
      ok(
        /region/i.test(html) && /site/i.test(html),
        `expected the missing-names message alongside it, got: ${html}`
      );
      ok(sandbox.document.getElementById("download").disabled === true);
    }
  );

  // =======================================================================
  // Task 18, item 7: the folder preview must be the server's exact
  // composed path, never a guess assembled client-side. The brief calls
  // this the one thing not to get wrong.
  // =======================================================================

  await test(
    "the folder preview shows the server's exact path once a full estimate succeeds",
    async () => {
      const FOLDER = "C:\\Users\\Param\\Surveys\\Vale of Glamorgan\\2026-08-02_Barry-Waterfront";
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/estimate") {
          return jsonResponse(200, {
            tiles: 4,
            rows: 2,
            cols: 2,
            extent_km: { width: 1, height: 1 },
            bytes_estimate: 5000000,
            seconds_estimate: 120,
            warnings: [],
            folder: FOLDER,
          });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "Vale of Glamorgan");
      setField(sandbox, "site", "Barry Waterfront");
      await flush(10);
      ok(sandbox.document.getElementById("folder-preview").hidden === false, "expected the folder preview visible");
      ok(
        sandbox.document.getElementById("folder-preview-path").textContent === FOLDER,
        `expected the exact server path, got: ${sandbox.document.getElementById("folder-preview-path").textContent}`
      );
    }
  );

  await test(
    "the displayed folder is the server's value verbatim, not a client-side guess from region/site text",
    async () => {
      // A region/site pair that would slugify non-trivially if anything
      // here tried to reconstruct the path itself. The mocked folder is
      // deliberately unrelated to that text: if the rendered value were
      // ever derived from "Chateau-sur-Mer" rather than displayed exactly
      // as given, that would mean app.js is assembling its own path
      // somewhere instead of only ever showing what naming.
      // build_package_paths actually composed.
      const UNGUESSABLE_FOLDER = "Z:\\unrelated\\path\\that\\proves\\nothing\\is\\being\\guessed";
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/estimate") {
          return jsonResponse(200, {
            tiles: 1,
            rows: 1,
            cols: 1,
            extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000,
            seconds_estimate: 60,
            warnings: [],
            folder: UNGUESSABLE_FOLDER,
          });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "Chateau-sur-Mer");
      setField(sandbox, "site", "Quai d'Ete");
      await flush(10);
      ok(
        sandbox.document.getElementById("folder-preview-path").textContent === UNGUESSABLE_FOLDER,
        `expected the verbatim server value, got: ${sandbox.document.getElementById("folder-preview-path").textContent}`
      );
    }
  );

  await test("the folder preview hides again once the estimate becomes invalid", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          warnings: [],
          folder: "C:\\Surveys\\R\\2026-08-02_S",
        });
      }
      if (url.pathname === "/api/extent") {
        return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "R");
    setField(sandbox, "site", "S");
    await flush(10);
    ok(sandbox.document.getElementById("folder-preview").hidden === false, "expected the preview visible first");
    setField(sandbox, "site", "");
    await flush(10);
    ok(
      sandbox.document.getElementById("folder-preview").hidden === true,
      "expected the preview to hide once site is cleared"
    );
  });

  // =======================================================================
  // Task 18, item 5: the elevation API key. Ticked by default alongside
  // every other source, a warning shown rather than the layer silently
  // switched off, the key persisted and prefilled, and never present in
  // a job's own request payload.
  // =======================================================================

  await test("every source, elevation included, is ticked by default", async () => {
    const stub = makeFetchStub(async (url) => {
      if (url.pathname === "/api/config") return jsonResponse(200, DEFAULT_CONFIG);
      if (url.pathname === "/api/categories") return jsonResponse(200, DEFAULT_CATEGORIES);
      if (url.pathname === "/api/sources") {
        return jsonResponse(200, [
          { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false },
          { id: "overture", display_name: "Overture Maps", licence: "ODbL", requires_api_key: false },
          {
            id: "elevation",
            display_name: "Elevation (OpenTopography COP30)",
            licence: "Copernicus DEM",
            requires_api_key: true,
            api_key_config_field: "opentopography_api_key",
          },
        ]);
      }
      return null;
    });
    const sandbox = buildSandbox({ fetch: stub.fetch });
    await flush(10);
    const html = sandbox.document.getElementById("sources").innerHTML;
    const checkboxCount = (html.match(/<input type="checkbox"/g) || []).length;
    const checkedCount = (html.match(/checked/g) || []).length;
    ok(checkboxCount === 3, `expected 3 source checkboxes, got ${checkboxCount}`);
    ok(
      checkedCount === 3,
      `expected all 3 sources ticked by default (the owner's ruling was not to untick ` +
        `elevation for lacking a key), got ${checkedCount} checked: ${html}`
    );
  });

  await test(
    "unticking a source checkbox excludes it from the job's payload (closes a long-standing harness gap)",
    async () => {
      // Task 16 documented this as an honest limit of the DOM mock at
      // the time: querySelectorAll was stubbed to always return [], so
      // payload().sources was always empty and source selection was
      // "entirely unverified until someone clicks it" in a real browser.
      // The mock now parses a rendered checklist's real <input> tags, so
      // this is provable directly.
      // Built directly rather than via bootedSandbox: bootRoutes() answers
      // /api/sources with the fixed single-entry DEFAULT_SOURCES before an
      // extra route ever gets a look in, which is exactly wrong for a test
      // that needs two sources to tell apart.
      const stub = makeFetchStub(async (url) => {
        if (url.pathname === "/api/config") return jsonResponse(200, DEFAULT_CONFIG);
        if (url.pathname === "/api/categories") return jsonResponse(200, DEFAULT_CATEGORIES);
        if (url.pathname === "/api/sources") {
          return jsonResponse(200, [
            { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false },
            { id: "overture", display_name: "Overture Maps", licence: "ODbL", requires_api_key: false },
          ]);
        }
        return null;
      });
      const sandbox = buildSandbox({ fetch: stub.fetch });
      await flush(10);
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39"); // payload() needs a bbox to read
      const checkboxes = sandbox.document.querySelectorAll("#sources input");
      ok(checkboxes.length === 2, `expected 2 rendered source checkboxes, got ${checkboxes.length}`);
      const overtureBox = checkboxes.find((c) => c.value === "overture");
      overtureBox.checked = false;
      ok(sandbox.payload().sources.sort().join(",") === "osm", `expected only osm, got ${sandbox.payload().sources}`);
    }
  );

  await test(
    "source metadata (id, display_name, licence) is HTML-escaped when rendered",
    async () => {
      // Review round 2: this code change shipped in round 1 with no
      // dedicated test at all, so reverting the escaping passed every
      // check in both suites. /api/sources is this server's own data,
      // not third-party input, but the escaping is cheap and this closes
      // the actual gap: nothing previously exercised it.
      const stub = makeFetchStub(async (url) => {
        if (url.pathname === "/api/config") return jsonResponse(200, DEFAULT_CONFIG);
        if (url.pathname === "/api/categories") return jsonResponse(200, DEFAULT_CATEGORIES);
        if (url.pathname === "/api/sources") {
          return jsonResponse(200, [
            {
              id: 'osm"><script>alert(1)</script>',
              display_name: "<b>OpenStreetMap</b>",
              licence: 'ODbL" onmouseover="alert(1)',
              requires_api_key: false,
            },
          ]);
        }
        return null;
      });
      const sandbox = buildSandbox({ fetch: stub.fetch });
      await flush(10);
      const html = sandbox.document.getElementById("sources").innerHTML;
      ok(!html.includes("<script>"), `expected the id's script tag escaped, got: ${html}`);
      ok(!html.includes("<b>OpenStreetMap</b>"), `expected the display_name's tag escaped, got: ${html}`);
      ok(
        !html.includes('onmouseover="alert(1)"'),
        `expected the licence's attribute breakout escaped, got: ${html}`
      );
    }
  );

  // =======================================================================
  // Task 19, item 3: the category checklist. Rendered from GET /api/
  // categories (a leaf group gets its own checkbox; a group with children,
  // "roads" today, is a label over its children instead), ticked by
  // default, and reaching payload().categories the same way #sources
  // reaches payload().sources.
  // =======================================================================

  await test("categories render one checkbox per leaf, all ticked by default", async () => {
    const { sandbox } = await bootedSandbox();
    const boxes = sandbox.document.querySelectorAll("#categories input");
    // DEFAULT_CATEGORIES: "buildings" (leaf) plus "roads" with two
    // children (motorway, footpath) = 3 leaf checkboxes total.
    ok(boxes.length === 3, `expected 3 leaf checkboxes, got ${boxes.length}`);
    ok(boxes.every((b) => b.checked === true), "expected every category ticked by default");
    ok(
      boxes.map((b) => b.value).sort().join(",") === "buildings,footpath,motorway",
      `unexpected values: ${boxes.map((b) => b.value)}`
    );
  });

  await test("\"roads\" itself is never a selectable checkbox value, only its children", async () => {
    const { sandbox } = await bootedSandbox();
    const boxes = sandbox.document.querySelectorAll("#categories input");
    ok(!boxes.some((b) => b.value === "roads"), "expected no checkbox valued \"roads\"");
    const html = sandbox.document.getElementById("categories").innerHTML;
    ok(html.includes("Roads"), `expected the "Roads" group label rendered, got: ${html}`);
  });

  await test("unticking a category checkbox excludes it from the job's payload", async () => {
    const { sandbox } = await bootedSandbox();
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    const boxes = sandbox.document.querySelectorAll("#categories input");
    const footpathBox = boxes.find((b) => b.value === "footpath");
    footpathBox.checked = false;
    const selected = sandbox.payload().categories;
    ok(!selected.includes("footpath"), `expected footpath excluded, got: ${selected}`);
    ok(selected.includes("buildings") && selected.includes("motorway"), `expected the rest still included, got: ${selected}`);
  });

  await test("every category still selected sends the full set, not an omitted key", async () => {
    // The server treats an ABSENT categories key as "everything" and a
    // present empty array as "nothing"; this page must always send the
    // explicit, current list, never omit the key just because nothing
    // has been deselected.
    const { sandbox } = await bootedSandbox();
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    ok("categories" in sandbox.payload(), "expected a categories key present");
    ok(sandbox.payload().categories.length === 3);
  });

  await test("changing a category checkbox refreshes the estimate", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000, seconds_estimate: 60, warnings: [], folder: "C:\\out",
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    setField(sandbox, "region", "R");
    setField(sandbox, "site", "S");
    await flush(10);
    fetchCalls.length = 0;
    const boxes = sandbox.document.querySelectorAll("#categories input");
    boxes.find((b) => b.value === "buildings").checked = false;
    sandbox.document.getElementById("categories").fire("change");
    await flush(10);
    ok(fetchCalls.some((c) => c.url.pathname === "/api/estimate"), "expected a fresh estimate after changing categories");
  });

  // =======================================================================
  // Task 21, defect 1: unticking every category used to be accepted right
  // through to the server (which now refuses it, see
  // mapgen.categories.EmptyCategorySelectionError), so the owner only found
  // out after pressing Download and getting an error back. This is the
  // browser-side half of that fix: reach the same disabled-Download state a
  // missing site name already produces, before an estimate is ever
  // attempted, never merely surface the server's rejection after the fact.
  // =======================================================================

  await test(
    "unticking every category checkbox disables Download with a visible reason",
    async () => {
      let allowEstimate = true;
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
        }
        if (url.pathname === "/api/estimate") {
          // Only ever answered for the initial, fully-ticked estimate
          // below. Once every category is unticked, this test flips
          // allowEstimate to false: if the page still reached this route
          // with nothing selected, that is the client-side guard failing
          // to do its one job, so the unhandled-fetch error is the right
          // way for this test to fail, not a quiet 200 that would hide it.
          if (!allowEstimate) return null;
          return jsonResponse(200, {
            tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000, seconds_estimate: 60, warnings: [], folder: "C:\\out",
          });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      setField(sandbox, "region", "R");
      setField(sandbox, "site", "S");
      await flush(10);
      ok(sandbox.document.getElementById("download").disabled === false, "expected a valid estimate first");

      allowEstimate = false;
      fetchCalls.length = 0;
      const boxes = sandbox.document.querySelectorAll("#categories input");
      boxes.forEach((box) => {
        box.checked = false;
      });
      sandbox.document.getElementById("categories").fire("change");
      await flush(10);

      ok(
        sandbox.document.getElementById("download").disabled === true,
        "expected Download disabled once every category is unticked"
      );
      const message = sandbox.document.getElementById("estimate").textContent
        || sandbox.document.getElementById("estimate").innerHTML;
      ok(/categor/i.test(message), `expected the reason to mention categories, got: ${message}`);
      ok(
        !fetchCalls.some((c) => c.url.pathname === "/api/estimate"),
        "expected no /api/estimate call while nothing is selected"
      );
    }
  );

  await test(
    "re-ticking a category after unticking every one re-enables Download",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
        }
        if (url.pathname === "/api/estimate") {
          return jsonResponse(200, {
            tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000, seconds_estimate: 60, warnings: [], folder: "C:\\out",
          });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      setField(sandbox, "region", "R");
      setField(sandbox, "site", "S");
      await flush(10);
      const boxes = sandbox.document.querySelectorAll("#categories input");
      boxes.forEach((box) => {
        box.checked = false;
      });
      sandbox.document.getElementById("categories").fire("change");
      await flush(10);
      ok(sandbox.document.getElementById("download").disabled === true);

      boxes[0].checked = true;
      sandbox.document.getElementById("categories").fire("change");
      await flush(10);
      ok(
        sandbox.document.getElementById("download").disabled === false,
        "expected Download re-enabled once a category is ticked again"
      );
    }
  );

  await test(
    "unticking every LAYER checkbox disables Download with a visible reason",
    async () => {
      // Review finding C1. The layer checklist had no client-side guard
      // at all: missingFieldsMessage() checked the extent, the names and
      // the categories and never the layers, so Download stayed enabled,
      // the page sent sources: [], and the server coalesced that back
      // into the full default pair and downloaded both.
      let allowEstimate = true;
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
        }
        if (url.pathname === "/api/estimate") {
          // Answered only for the fully-ticked estimate below, for the
          // same reason the category test above does it: reaching this
          // route with no layer selected is the guard failing, and an
          // unhandled-fetch error is the right way for that to fail.
          if (!allowEstimate) return null;
          return jsonResponse(200, {
            tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000, seconds_estimate: 60, warnings: [], folder: "C:\\out",
          });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      setField(sandbox, "region", "R");
      setField(sandbox, "site", "S");
      await flush(10);
      ok(sandbox.document.getElementById("download").disabled === false, "expected a valid estimate first");

      allowEstimate = false;
      fetchCalls.length = 0;
      const boxes = sandbox.document.querySelectorAll("#sources input");
      ok(boxes.length > 0, "expected the layer checklist to have rendered");
      boxes.forEach((box) => {
        box.checked = false;
      });
      sandbox.document.getElementById("sources").fire("change");
      await flush(10);

      ok(
        sandbox.document.getElementById("download").disabled === true,
        "expected Download disabled once every layer is unticked"
      );
      const message = sandbox.document.getElementById("estimate").textContent
        || sandbox.document.getElementById("estimate").innerHTML;
      ok(/layer/i.test(message), `expected the reason to mention layers, got: ${message}`);
      ok(
        !fetchCalls.some((c) => c.url.pathname === "/api/estimate"),
        "expected no /api/estimate call while no layer is selected"
      );

      // And back: re-ticking one layer re-enables Download, so the guard
      // is a guard rather than a one-way trap.
      allowEstimate = true;
      boxes[0].checked = true;
      sandbox.document.getElementById("sources").fire("change");
      await flush(10);
      ok(
        sandbox.document.getElementById("download").disabled === false,
        "expected Download re-enabled once a layer is ticked again"
      );
    }
  );

  // =======================================================================
  // Task 19, item 2: the settings panel. API key fields are rendered from
  // the source registry (GET /api/sources' api_key_config_field), one per
  // keyed source, rather than a single hard-coded field, so a second or
  // third keyed source in phase 2 needs no new markup or JS here.
  // =======================================================================

  const ELEVATION_SOURCE_ENTRY = {
    id: "elevation",
    display_name: "Elevation (OpenTopography COP30)",
    licence: "Copernicus DEM",
    requires_api_key: true,
    api_key_config_field: "opentopography_api_key",
  };

  await test("a source with no api_key_config_field renders no key field at all", async () => {
    const { sandbox } = await bootedSandbox();
    // DEFAULT_SOURCES' one entry has requires_api_key: false and no
    // api_key_config_field: renderApiKeys must not render anything for it.
    ok(sandbox.document.getElementById("api-keys").innerHTML.trim() === "");
  });

  await test("boot() pre-fills a keyed source's field from the saved config", async () => {
    const stub = makeFetchStub(async (url) => {
      if (url.pathname === "/api/config") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-saved-key" });
      }
      if (url.pathname === "/api/categories") return jsonResponse(200, DEFAULT_CATEGORIES);
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      return null;
    });
    const sandbox = buildSandbox({ fetch: stub.fetch });
    await flush(10);
    const field = sandbox.document.querySelector(
      '#api-keys input[data-config-field="opentopography_api_key"]'
    );
    ok(field, "expected a rendered field for opentopography_api_key");
    ok(field.value === "sk-saved-key", `expected the saved key prefilled, got: ${field.value}`);
    ok(field.type === "password", `expected type="password", got: ${field.type}`);
  });

  // =======================================================================
  // Task 21, defect 2: a saved key and an unsaved one both render as the
  // same row of dots in a type="password" field, which is what convinced
  // the owner a save that had genuinely worked had not. mapgen.config's
  // persistence is unchanged and correct (see the Python side of this
  // fix); what was missing is an honest, visible "saved" signal that
  // updates when a key is actually saved or cleared, without ever
  // echoing the key itself.
  //
  // isSaved() below reads the api-key-status element's own class rather
  // than matching "key saved"/"no key saved" as plain text: "no key
  // saved" itself contains "key saved" as a substring, so a naive text
  // match cannot tell the two states apart on its own.
  // =======================================================================

  function isSaved(sandbox) {
    const html = sandbox.document.getElementById("api-keys").innerHTML;
    const saved = html.includes('class="api-key-status saved"');
    const unsaved = html.includes('class="api-key-status unsaved"');
    ok(saved || unsaved, `expected a rendered saved/unsaved indicator, got: ${html}`);
    ok(!(saved && unsaved), `expected exactly one indicator state, got both: ${html}`);
    return saved;
  }

  await test("no saved key renders a 'no key saved' indicator, not a claim of saved", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      return null;
    });
    ok(isSaved(sandbox) === false, "expected the unsaved indicator with an empty config value");
    ok(/no key saved/i.test(sandbox.document.getElementById("api-keys").innerHTML));
  });

  await test("a key already saved on disk renders a 'key saved' indicator", async () => {
    const stub = makeFetchStub(async (url) => {
      if (url.pathname === "/api/config") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-saved-key" });
      }
      if (url.pathname === "/api/categories") return jsonResponse(200, DEFAULT_CATEGORIES);
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      return null;
    });
    const sandbox = buildSandbox({ fetch: stub.fetch });
    await flush(10);
    ok(isSaved(sandbox) === true, "expected the saved indicator when config already has a key");
  });

  await test("saving a new key updates the indicator from unsaved to saved", async () => {
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-new-key" });
      }
      return null;
    });
    ok(isSaved(sandbox) === false);
    setApiKeyField(sandbox, "opentopography_api_key", "sk-new-key");
    await flush(10);
    ok(isSaved(sandbox) === true, "expected the indicator to flip to saved once the PUT resolves");
  });

  await test("clearing a saved key updates the indicator from saved back to unsaved", async () => {
    const stub = makeFetchStub(async (url, options) => {
      if (url.pathname === "/api/config" && (!options.method || options.method === "GET")) {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-saved-key" });
      }
      if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "" });
      }
      if (url.pathname === "/api/categories") return jsonResponse(200, DEFAULT_CATEGORIES);
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      return null;
    });
    const sandbox = buildSandbox({ fetch: stub.fetch });
    await flush(10);
    ok(isSaved(sandbox) === true);
    setApiKeyField(sandbox, "opentopography_api_key", "");
    await flush(10);
    ok(isSaved(sandbox) === false, "expected the indicator to flip back to unsaved once cleared and saved");
  });

  await test("a failed save leaves the indicator reflecting the last confirmed state, not the unsaved attempt", async () => {
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
        return jsonResponse(500, { error: "disk full" });
      }
      return null;
    });
    ok(isSaved(sandbox) === false);
    setApiKeyField(sandbox, "opentopography_api_key", "sk-attempted-key");
    await flush(10);
    ok(
      isSaved(sandbox) === false,
      "expected the indicator to still say unsaved after a failed PUT, rather than claim the attempted value was saved"
    );
  });

  await test("a keyed source's field persists immediately on change, with no estimate to validate it against", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-new-key" });
      }
      return null;
    });
    fetchCalls.length = 0;
    setApiKeyField(sandbox, "opentopography_api_key", "sk-new-key");
    await flush(10);
    const putCall = fetchCalls.find((c) => (c.options.method || "").toUpperCase() === "PUT");
    ok(putCall, "expected an immediate PUT /api/config call");
    const body = JSON.parse(putCall.options.body);
    ok(body.opentopography_api_key === "sk-new-key", `unexpected PUT body: ${putCall.options.body}`);
  });

  await test("the settings panel starts hidden and opens on the Settings button", async () => {
    const { sandbox } = await bootedSandbox();
    ok(sandbox.document.getElementById("settings-panel").hidden === true, "expected hidden initially");
    sandbox.document.getElementById("settings-toggle").fire("click");
    ok(sandbox.document.getElementById("settings-panel").hidden === false, "expected visible after Settings");
  });

  await test("the settings panel closes on its Close button", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("settings-toggle").fire("click");
    sandbox.document.getElementById("settings-close").fire("click");
    ok(sandbox.document.getElementById("settings-panel").hidden === true);
  });

  await test("the settings panel closes on clicking its backdrop", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("settings-toggle").fire("click");
    sandbox.document.getElementById("settings-backdrop").fire("click");
    ok(sandbox.document.getElementById("settings-panel").hidden === true);
  });

  await test(
    "the API key change handler awaits the save before refreshing, not racing it",
    async () => {
      // Review round 2: the previous version of this test asserted on
      // DISPATCH order, which is set the moment each fetch() call is
      // made, synchronously, regardless of whether the code actually
      // awaits anything in between; removing the await reproduces the
      // exact race and dispatch order is unchanged, so that version
      // passed for a mutation it was meant to catch. This instead makes
      // the PUT's own resolution controllable and checks what has been
      // dispatched at each point in time: with the await in place,
      // nothing calls /api/estimate until the PUT actually resolves.
      let resolvePut;
      const putResponsePromise = new Promise((resolve) => {
        resolvePut = resolve;
      });
      const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
        if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
          return putResponsePromise;
        }
        if (url.pathname === "/api/estimate") {
          return jsonResponse(200, {
            tiles: 1,
            rows: 1,
            cols: 1,
            extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000,
            seconds_estimate: 60,
            warnings: [],
            folder: "C:\\out",
          });
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "R");
      setField(sandbox, "site", "S");
      await flush(10);
      fetchCalls.length = 0;
      setApiKeyField(sandbox, "opentopography_api_key", "sk-new-key");
      await flush(10); // long enough for the change handler to reach its awaited PUT
      ok(
        fetchCalls.some((c) => c.url.pathname === "/api/config"),
        "expected the PUT to have been dispatched"
      );
      ok(
        !fetchCalls.some((c) => c.url.pathname === "/api/estimate"),
        "expected refreshEstimate NOT to have run yet: the PUT has not resolved"
      );
      resolvePut(jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-new-key" }));
      await flush(10);
      ok(
        fetchCalls.some((c) => c.url.pathname === "/api/estimate"),
        "expected refreshEstimate to run once the PUT resolved"
      );
    }
  );

  await test("an estimate warning is shown alongside the numbers without disabling download", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          warnings: ["Elevation is selected but no OpenTopography API key is configured."],
          folder: "C:\\Surveys\\R\\2026-08-02_S",
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "R");
    setField(sandbox, "site", "S");
    await flush(10);
    const html = sandbox.document.getElementById("estimate").innerHTML;
    ok(/API key/i.test(html), `expected the warning text in the estimate panel, got: ${html}`);
    ok(
      sandbox.document.getElementById("download").disabled === false,
      "a readiness warning must not disable an otherwise-valid download"
    );
  });

  // =======================================================================
  // Task 8 of the OS Open tier resolver plan: the tier list, rendering
  // resolve()'s own output (resolver.py) as one line per category beside
  // the estimate's numbers.
  // =======================================================================

  await test(
    "renderResolution renders one line per category: a bare base name, a filled-by partial entry, and a reference entry",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.renderResolution([
        {
          category: "buildings",
          sources: [
            { id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" },
            { id: "os_open", display_name: "OS Open", tier: 2, coverage: "partial", role: "fill" },
          ],
        },
        {
          category: "roads",
          sources: [
            { id: "os_open", display_name: "OS Open Roads", tier: 1, coverage: "full", role: "reference" },
          ],
        },
      ]);
      const box = sandbox.document.getElementById("tier-list");
      ok(box.hidden === false, "expected the tier list to be shown");
      const expected =
        "<div>buildings: OpenStreetMap, filled by OS Open (partial coverage here)</div>" +
        "<div>roads: reference: OS Open Roads</div>";
      ok(box.innerHTML === expected, `unexpected tier list markup:\n  got:      ${box.innerHTML}\n  expected: ${expected}`);
    }
  );

  await test(
    "category labels are humanised per the brief's table; any other category renders as its own string",
    async () => {
      const { sandbox } = await bootedSandbox();
      const oneEntry = (category) => ({
        category,
        sources: [{ id: "os_open", display_name: "OS Open", tier: 1, coverage: "full", role: "base" }],
      });
      sandbox.renderResolution([
        oneEntry("land_use"),
        oneEntry("heights"),
        oneEntry("land"),
        oneEntry("sites"),
        oneEntry("places"),
        oneEntry("terrain"),
      ]);
      const html = sandbox.document.getElementById("tier-list").innerHTML;
      ok(html.includes("<div>land use: OS Open</div>"), `land_use not humanised, got: ${html}`);
      ok(html.includes("<div>building heights: OS Open</div>"), `heights not humanised, got: ${html}`);
      ok(html.includes("<div>woodland and water: OS Open</div>"), `land not humanised, got: ${html}`);
      ok(html.includes("<div>functional sites: OS Open</div>"), `sites not humanised, got: ${html}`);
      ok(html.includes("<div>place names: OS Open</div>"), `places not humanised, got: ${html}`);
      ok(html.includes("<div>terrain: OS Open</div>"), `expected an unlisted category to render as-is, got: ${html}`);
    }
  );

  await test(
    "renderResolution renders nothing for an empty list or an absent one, clearing any previous list identically",
    async () => {
      const { sandbox } = await bootedSandbox();
      const oneCategory = [
        {
          category: "buildings",
          sources: [{ id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" }],
        },
      ];

      sandbox.renderResolution(oneCategory);
      ok(sandbox.document.getElementById("tier-list").hidden === false, "expected a rendered list first");

      sandbox.renderResolution([]);
      let box = sandbox.document.getElementById("tier-list");
      ok(box.hidden === true, "an empty list must hide the box");
      ok(box.innerHTML === "", `an empty list must clear the previous markup, got: ${box.innerHTML}`);

      sandbox.renderResolution(oneCategory);
      ok(sandbox.document.getElementById("tier-list").hidden === false, "expected a rendered list again");

      sandbox.renderResolution(undefined);
      box = sandbox.document.getElementById("tier-list");
      ok(box.hidden === true, "an absent resolution must hide the box exactly as an empty one does");
      ok(box.innerHTML === "", `an absent resolution must clear the previous markup, got: ${box.innerHTML}`);
    }
  );

  await test(
    "a degraded selection: the best PRESENT entry renders bare even when its own role field says \"fill\", " +
      "and a category left with only reference entries reads as reference alone",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.renderResolution([
        {
          // osm deselected: os_open is the best entry PRESENT for this
          // bbox and this selection, but its own role field still reads
          // "fill" (task-8-brief.md's degraded-selection case). The tier
          // list's own "base" is POSITIONAL, sources[0], never a search
          // for role === "base", so this still renders with no prefix on
          // itself and the remaining entries as fills after it.
          category: "buildings",
          sources: [
            { id: "os_open", display_name: "OS Open", tier: 2, coverage: "full", role: "fill" },
            { id: "overture", display_name: "Overture", tier: 3, coverage: "full", role: "fill" },
          ],
        },
        {
          // roads with osm deselected and os_open's role forced to
          // "reference" by resolver.py's own ROLES table: the only entry
          // left IS a reference, and the line says so rather than
          // inventing a base name role never assigned this category.
          category: "roads",
          sources: [
            { id: "os_open", display_name: "OS Open Roads", tier: 1, coverage: "full", role: "reference" },
          ],
        },
      ]);
      const html = sandbox.document.getElementById("tier-list").innerHTML;
      ok(
        html.includes("<div>buildings: OS Open, filled by Overture</div>"),
        `expected the degraded base with no "filled by" on itself, got: ${html}`
      );
      ok(
        html.includes("<div>roads: reference: OS Open Roads</div>"),
        `expected an all-reference category to read as reference alone, got: ${html}`
      );
    }
  );

  // =======================================================================
  // Phase 2b item C, Task 3: the base entry's own "detail" field, when
  // present, renders comma-joined after the base name and before
  // everything else on the line. Only sources[0]'s detail is ever shown;
  // a fill or reference entry's detail stays payload-only, since showing
  // every entry's detail would triple the line for what the package
  // actually gets, which is the base's quality.
  // =======================================================================

  await test(
    "a base entry's detail renders comma-joined after its name and before any filled-by segment",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.renderResolution([
        {
          category: "terrain",
          sources: [
            {
              id: "lidar_wales",
              display_name: "LiDAR terrain (Wales, 1 m)",
              tier: 1,
              coverage: "full",
              role: "base",
              detail: "2 m at this extent (extents under about 4 x 4 km come back at 1 m)",
            },
            {
              id: "copernicus_glo30",
              display_name: "Elevation (Copernicus GLO-30)",
              tier: 2,
              coverage: "full",
              role: "fill",
            },
          ],
        },
      ]);
      const html = sandbox.document.getElementById("tier-list").innerHTML;
      const expected =
        "<div>terrain: LiDAR terrain (Wales, 1 m), " +
        "2 m at this extent (extents under about 4 x 4 km come back at 1 m), " +
        "filled by Elevation (Copernicus GLO-30)</div>";
      ok(html === expected, `unexpected tier list markup:\n  got:      ${html}\n  expected: ${expected}`);
    }
  );

  await test(
    "a base entry with no detail renders exactly as today, even when a FILL entry beside it has one",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.renderResolution([
        {
          category: "buildings",
          sources: [
            { id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" },
            {
              id: "os_open",
              display_name: "OS Open",
              tier: 2,
              coverage: "partial",
              role: "fill",
              detail: "should never reach the rendered line",
            },
          ],
        },
      ]);
      const html = sandbox.document.getElementById("tier-list").innerHTML;
      const expected = "<div>buildings: OpenStreetMap, filled by OS Open (partial coverage here)</div>";
      ok(html === expected, `unexpected tier list markup:\n  got:      ${html}\n  expected: ${expected}`);
      ok(
        !html.includes("should never reach"),
        `a fill entry's own detail must not render, got: ${html}`
      );
    }
  );

  await test(
    "a base entry with both partial coverage and detail orders as name, coverage caveat, then detail",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.renderResolution([
        {
          category: "terrain",
          sources: [
            {
              id: "lidar_wales",
              display_name: "LiDAR terrain (Wales, 1 m)",
              tier: 1,
              coverage: "partial",
              role: "base",
              detail: "2 m at this extent",
            },
          ],
        },
      ]);
      const html = sandbox.document.getElementById("tier-list").innerHTML;
      const expected =
        "<div>terrain: LiDAR terrain (Wales, 1 m) (partial coverage here), 2 m at this extent</div>";
      ok(html === expected, `unexpected tier list markup:\n  got:      ${html}\n  expected: ${expected}`);
    }
  );

  await test("a hostile detail string is HTML-escaped before being rendered", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.renderResolution([
      {
        category: "terrain",
        sources: [
          {
            id: "lidar_wales",
            display_name: "LiDAR terrain",
            tier: 1,
            coverage: "full",
            role: "base",
            detail: "<script>alert(1)</script> & Sons",
          },
        ],
      },
    ]);
    const html = sandbox.document.getElementById("tier-list").innerHTML;
    ok(!html.includes("<script>"), `expected the raw tag in detail to be escaped, got: ${html}`);
    ok(html.includes("&lt;script&gt;"), `expected an escaped form present, got: ${html}`);
  });

  function estimateResponse(resolution) {
    return {
      tiles: 1,
      rows: 1,
      cols: 1,
      extent_km: { width: 1, height: 1 },
      bytes_estimate: 1000,
      seconds_estimate: 60,
      warnings: [],
      folder: "C:\\Surveys\\R\\2026-08-06_S",
      ...(resolution === undefined ? {} : { resolution }),
    };
  }

  await test("refreshEstimate populates the tier list from the estimate response's own resolution field", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(
          200,
          estimateResponse([
            {
              category: "buildings",
              sources: [{ id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" }],
            },
          ])
        );
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "R");
    setField(sandbox, "site", "S");
    await flush(10);
    const box = sandbox.document.getElementById("tier-list");
    ok(box.hidden === false, "expected the tier list populated from a successful estimate");
    ok(
      box.innerHTML === "<div>buildings: OpenStreetMap</div>",
      `unexpected tier list markup: ${box.innerHTML}`
    );
  });

  await test("a new estimate replaces the tier list rather than appending to it", async () => {
    let call = 0;
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        call += 1;
        const source =
          call === 1
            ? { id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" }
            : { id: "os_open", display_name: "OS Open", tier: 1, coverage: "full", role: "base" };
        return jsonResponse(200, estimateResponse([{ category: "buildings", sources: [source] }]));
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "R");
    setField(sandbox, "site", "S");
    await flush(10);
    ok(
      sandbox.document.getElementById("tier-list").innerHTML === "<div>buildings: OpenStreetMap</div>",
      `unexpected markup after the first estimate: ${sandbox.document.getElementById("tier-list").innerHTML}`
    );

    setField(sandbox, "overlap", "250");
    await flush(10);
    const html = sandbox.document.getElementById("tier-list").innerHTML;
    ok(
      html === "<div>buildings: OS Open</div>",
      `expected the first estimate's entry replaced, not kept alongside the second, got: ${html}`
    );
  });

  await test(
    "an estimate with no resolution key clears a tier list a previous estimate drew (absent and empty read the same)",
    async () => {
      let call = 0;
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/estimate") {
          call += 1;
          return jsonResponse(
            200,
            estimateResponse(
              call === 1
                ? [
                    {
                      category: "buildings",
                      sources: [
                        { id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" },
                      ],
                    },
                  ]
                : undefined
            )
          );
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "R");
      setField(sandbox, "site", "S");
      await flush(10);
      ok(sandbox.document.getElementById("tier-list").hidden === false, "expected the first estimate to populate it");

      setField(sandbox, "overlap", "250");
      await flush(10);
      const box = sandbox.document.getElementById("tier-list");
      ok(box.hidden === true, "an older-shaped response with no resolution key must clear the list, not keep the stale one");
      ok(box.innerHTML === "", `expected the stale markup cleared, got: ${box.innerHTML}`);
    }
  );

  await test("a failed estimate clears the tier list a previous successful estimate drew", async () => {
    let call = 0;
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        call += 1;
        if (call === 1) {
          return jsonResponse(
            200,
            estimateResponse([
              {
                category: "buildings",
                sources: [{ id: "osm", display_name: "OpenStreetMap", tier: 1, coverage: "full", role: "base" }],
              },
            ])
          );
        }
        return jsonResponse(400, { error: "tiling is absurd for this extent" });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "R");
    setField(sandbox, "site", "S");
    await flush(10);
    ok(sandbox.document.getElementById("tier-list").hidden === false, "expected the first estimate to populate it");

    setField(sandbox, "overlap", "250");
    await flush(10);
    const box = sandbox.document.getElementById("tier-list");
    ok(box.hidden === true, "a failed re-estimate must clear a tier list the last successful one drew");
    ok(box.innerHTML === "", `expected the stale markup cleared, got: ${box.innerHTML}`);
  });

  // =======================================================================
  // GET /api/coverage, and the panel and the auto-selection it drives.
  //
  // This exists because a real survey went wrong. The owner drew Cowbridge
  // with the Cardiff 25 cm LiDAR ticked, an archive covering ten tiles at
  // St Fagans and nothing else, and the page reported honest non-coverage
  // as a failure: red tiles, a source_failed event, two tile_failed events.
  // Their instruction was to stop attempting what does not cover, stop
  // making them tick anything at all, and cut the panel down to "a
  // highlight to show the level of lidar detail, and that all elements can
  // be captured. if one cant list it below. nothing else."
  //
  // Every fixture below is a real /api/coverage body shape, and every
  // detail string is one lidar_wales.detail or lidar_cardiff.detail
  // actually composes, because the panel reads the LEVEL out of that prose
  // and a fixture that invented its own wording would prove nothing about
  // the sentences those two really write.
  // =======================================================================

  const COVERAGE_SOURCE_REGISTRY = [
    { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false },
    {
      id: "os_uprn",
      display_name: "Addresses (OS Open UPRN, GB)",
      licence: "OGL",
      requires_api_key: false,
    },
    {
      id: "inspire",
      display_name: "Property boundaries (INSPIRE)",
      licence: "OGL",
      requires_api_key: false,
    },
    {
      id: "lidar_wales",
      display_name: "LiDAR terrain (Wales, 1 m)",
      licence: "OGL",
      requires_api_key: false,
    },
    {
      id: "lidar_cardiff",
      // The real one, verbatim, because its length is the point: this is
      // what the bullet shortener has to cope with.
      display_name:
        "LiDAR terrain (St Fagans and St Georges-super-Ely, Cardiff, 25 cm, flown 2011)",
      licence: "OGL",
      requires_api_key: false,
    },
  ];

  function coverageEntry(id, coverage, detail, heavy) {
    return {
      id,
      coverage,
      heavy: Boolean(heavy),
      detail: detail === undefined ? null : detail,
    };
  }

  // The owner's own Cowbridge extent: everything national covers it, the
  // Wales 1 m LiDAR covers it, and the Cardiff 25 cm archive does not.
  function cowbridgeCoverage() {
    return [
      coverageEntry("osm", "full", "traced footprints and centrelines, typically 1 to 5 m positional accuracy"),
      coverageEntry("os_uprn", "full", "one point per addressable location", true),
      coverageEntry("inspire", "full", "registered title extents"),
      coverageEntry("lidar_wales", "full", "1 m at this extent"),
      coverageEntry("lidar_cardiff", "none", null, true),
    ];
  }

  const COVERAGE_ESTIMATE = {
    tiles: 1,
    rows: 1,
    cols: 1,
    extent_km: { width: 1, height: 1 },
    bytes_estimate: 1000,
    seconds_estimate: 60,
    warnings: [],
    folder: "C:\\Surveys\\R\\2026-08-12_S",
  };

  // A booted page whose /api/coverage answer can be changed between
  // extents, which is what item 4 of the brief is about: an extent moved
  // onto the 25 cm block gains that source silently, and moved off loses
  // it again.
  async function coverageSandbox(options = {}) {
    const state = {
      entries: options.entries || cowbridgeCoverage(),
      estimateSources: options.estimateSources || [],
      status: options.status || 200,
      asked: [],
    };
    const { sandbox, fetchCalls } = await bootedSandbox(async (url) => {
      if (url.pathname === "/api/sources") {
        return jsonResponse(200, options.sources || COVERAGE_SOURCE_REGISTRY);
      }
      if (url.pathname === "/api/coverage") {
        state.asked.push(url.searchParams.get("bbox"));
        if (state.status !== 200) {
          return jsonResponse(state.status, { error: "Coverage is unavailable." });
        }
        return jsonResponse(200, { sources: state.entries });
      }
      if (url.pathname === "/api/extent") {
        return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
      }
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, { ...COVERAGE_ESTIMATE, sources: state.estimateSources });
      }
      return null;
    });
    return { sandbox, fetchCalls, state };
  }

  const COWBRIDGE = "-3.453,51.46,-3.444,51.467";
  const ST_FAGANS = "-3.2774,51.4847,-3.2717,51.4883";

  // Tag-stripped, whitespace-collapsed text of one panel element, or ""
  // when it is hidden. Hidden has to read as empty rather than as its last
  // contents: "the list is not shown" and "the list is shown and says
  // nothing" are different claims and only one of them is the brief.
  function panelPart(sandbox, id) {
    const element = sandbox.document.getElementById(id);
    if (!element || element.hidden) return "";
    const markup = element.innerHTML || element.textContent || "";
    return String(markup).replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
  }

  function panelText(sandbox) {
    return ["coverage-lidar", "coverage-all", "coverage-notes"]
      .map((id) => panelPart(sandbox, id))
      .filter((part) => part)
      .join(" ");
  }

  function panelBullets(sandbox) {
    const notes = sandbox.document.getElementById("coverage-notes");
    if (notes.hidden) return [];
    return Array.from(notes.innerHTML.matchAll(/<li>([\s\S]*?)<\/li>/g), (match) => match[1].trim());
  }

  function tickedSources(sandbox) {
    return sandbox.document
      .querySelectorAll("#sources input:checked")
      .map((input) => input.value)
      .sort();
  }

  await test("the highlight names the 1 m level a Wales extent actually gets", async () => {
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: 1 m",
      `expected the level alone, got: ${JSON.stringify(panelPart(sandbox, "coverage-lidar"))}`
    );
  });

  await test("an extent too large for 1 m says 2 m, and what to draw for 1 m", async () => {
    // lidar_wales.detail's own wording at overview level 1, verbatim.
    const entries = cowbridgeCoverage();
    entries[3] = coverageEntry(
      "lidar_wales",
      "full",
      "2 m at this extent (extents under about 4 x 4 km come back at 1 m)"
    );
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    const highlight = panelPart(sandbox, "coverage-lidar");
    ok(
      highlight.startsWith("LiDAR: 2 m at this size"),
      `expected the level this size gets first, got: ${JSON.stringify(highlight)}`
    );
    ok(
      highlight.includes("draw under 4 km for 1 m"),
      `expected the qualifier as a fragment, got: ${JSON.stringify(highlight)}`
    );
    // A fragment, not a sentence: the source's own detail() sentence is
    // three times this long and saying it here is what the owner asked to
    // be rid of.
    ok(highlight.length <= 50, `the highlight has grown into prose: ${JSON.stringify(highlight)}`);
  });

  await test("an extent on the 25 cm block reads 25 cm, the finest that covers it", async () => {
    const entries = cowbridgeCoverage();
    entries[4] = coverageEntry("lidar_cardiff", "full", "25 cm at this extent, flown 2011", true);
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", ST_FAGANS);
    await flush(10);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: 25 cm",
      `expected 25 cm to win over the 1 m source beside it, got: ${JSON.stringify(
        panelPart(sandbox, "coverage-lidar")
      )}`
    );
  });

  await test("an over-budget 25 cm extent reports what it gets, and what a smaller one would", async () => {
    // lidar_cardiff.detail's own over-budget wording. The source SKIPS an
    // extent this size (see _skip_reason), so 25 cm must not be read as
    // what this extent gets: 1 m is, and 25 cm is one redraw away.
    const entries = cowbridgeCoverage();
    entries[4] = coverageEntry(
      "lidar_cardiff",
      "full",
      "25 cm needs an extent under about 600 x 600 m here (flown 2011)",
      true
    );
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", ST_FAGANS);
    await flush(10);
    const highlight = panelPart(sandbox, "coverage-lidar");
    ok(
      highlight.startsWith("LiDAR: 1 m at this size"),
      `expected the level actually delivered, not the conditional one, got: ${JSON.stringify(highlight)}`
    );
    ok(
      highlight.includes("draw under 600 m for 25 cm"),
      `expected the redraw fragment, got: ${JSON.stringify(highlight)}`
    );
  });

  await test("no LiDAR at all over this ground says so in the same one line", async () => {
    const entries = cowbridgeCoverage();
    entries[3] = coverageEntry("lidar_wales", "none", null);
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: none here",
      `got: ${JSON.stringify(panelPart(sandbox, "coverage-lidar"))}`
    );
  });

  await test("the confirmation line appears when every other layer covers the extent", async () => {
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      sandbox.document.getElementById("coverage-all").textContent === "All other layers available",
      `got: ${JSON.stringify(sandbox.document.getElementById("coverage-all").textContent)}`
    );
    ok(sandbox.document.getElementById("coverage-all").hidden === false, "expected it shown");
  });

  await test("the bullet list is absent, not empty, when nothing is unavailable", async () => {
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    const notes = sandbox.document.getElementById("coverage-notes");
    ok(notes.hidden === true, "expected no list at all when there is nothing to list");
    ok(notes.innerHTML === "", `expected no markup either, got: ${notes.innerHTML}`);
  });

  await test("a LiDAR archive that does not reach here is never bulleted: the highlight speaks for it", async () => {
    // The owner's exact complaint, in one check. Over Cowbridge the
    // Cardiff 25 cm archive covers nothing, and saying so on the panel
    // would put the noise they asked to be rid of back on the page under
    // a different name. "LiDAR: 1 m" is the whole truth about LiDAR here.
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(panelBullets(sandbox).length === 0, `expected no bullets, got: ${JSON.stringify(panelBullets(sandbox))}`);
    ok(
      !panelText(sandbox).toLowerCase().includes("cardiff"),
      `the panel named an archive that simply does not reach here: ${panelText(sandbox)}`
    );
  });

  await test("one bullet per unavailable layer, and the confirmation withdrawn", async () => {
    const entries = cowbridgeCoverage();
    entries[1] = coverageEntry("os_uprn", "none", null, true);
    entries[2] = coverageEntry("inspire", "none", null);
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    const bullets = panelBullets(sandbox);
    ok(bullets.length === 2, `expected exactly two bullets, got: ${JSON.stringify(bullets)}`);
    ok(
      bullets.includes("Addresses: not covered here"),
      `expected the shortened source name and a fragment of a reason, got: ${JSON.stringify(bullets)}`
    );
    ok(
      bullets.includes("Property boundaries: not covered here"),
      `got: ${JSON.stringify(bullets)}`
    );
    ok(
      sandbox.document.getElementById("coverage-all").hidden === true,
      "the confirmation is a claim, and it is not true with two layers missing"
    );
    // Fragments, not sentences: every bullet stays inside a handful of
    // words, which is the whole of the owner's format instruction.
    ok(
      bullets.every((bullet) => bullet.length <= 48),
      `a bullet has grown into prose: ${JSON.stringify(bullets)}`
    );
  });

  await test("a non-LiDAR source with partial coverage is bulleted, not shown as fully available", async () => {
    // The owner's own complaint: before this fix, a source that only
    // half-covers the extent (anything other than "none" or "full") was
    // treated exactly like "full" everywhere on this panel unless it
    // happened to be a LiDAR source, which already gets its own "part
    // only" qualifier from the highlight above. Not covered at all and
    // only partly covered are both reasons to withdraw "All other layers
    // available", and both now say so.
    const entries = cowbridgeCoverage();
    entries[2] = coverageEntry("inspire", "partial", "registered title extents");
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    const bullets = panelBullets(sandbox);
    ok(
      bullets.includes("Property boundaries: part only"),
      `expected inspire's own partial coverage bulleted, got: ${JSON.stringify(bullets)}`
    );
    ok(
      sandbox.document.getElementById("coverage-all").hidden === true,
      "the confirmation is a claim, and it is not true with one layer only partly covering"
    );
  });

  await test("auto-selection ticks every covering source, the heavyweight ones included", async () => {
    // The owner's own decision, taken after being shown the tradeoff: a
    // 619 MB national address file is selected without being ticked by
    // hand, and the panel tells them what it costs rather than making
    // them find the checkbox.
    const entries = cowbridgeCoverage();
    entries[4] = coverageEntry("lidar_cardiff", "full", "25 cm at this extent, flown 2011", true);
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", ST_FAGANS);
    await flush(10);
    ok(
      tickedSources(sandbox).join(",") === "inspire,lidar_cardiff,lidar_wales,os_uprn,osm",
      `expected every covering source ticked, got: ${tickedSources(sandbox)}`
    );
  });

  await test("a source reporting none is unticked and never reaches the server", async () => {
    const { fetchCalls, sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    ok(
      !tickedSources(sandbox).includes("lidar_cardiff"),
      `expected the uncovering archive unticked, got: ${tickedSources(sandbox)}`
    );
    ok(
      !sandbox.payload().sources.includes("lidar_cardiff"),
      `expected it out of the payload, got: ${sandbox.payload().sources}`
    );
    // And out of the estimate that was actually sent, which is the half
    // that matters: "we shouldnt be attempting to pull it if its not
    // selected".
    const estimate = fetchCalls.filter((call) => call.url.pathname === "/api/estimate").pop();
    ok(estimate, "expected an estimate once the names were filled in");
    ok(
      !JSON.parse(estimate.options.body).sources.includes("lidar_cardiff"),
      `the estimate asked for a source this extent is not served by: ${estimate.options.body}`
    );
  });

  await test("the estimate for an extent goes out only after coverage has corrected the ticks", async () => {
    // Ordering, not decoration. payload() reads the checkboxes, so an
    // estimate fired before the answer landed would price a source this
    // extent is known not to be served by, and a Download pressed on that
    // estimate would attempt it.
    const { fetchCalls, sandbox } = await coverageSandbox();
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(10);
    fetchCalls.length = 0;
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(20);
    const paths = fetchCalls.map((call) => call.url.pathname);
    ok(paths.includes("/api/coverage"), `expected a coverage call, got: ${paths}`);
    ok(paths.includes("/api/estimate"), `expected an estimate, got: ${paths}`);
    ok(
      paths.indexOf("/api/coverage") < paths.indexOf("/api/estimate"),
      `expected coverage first, got: ${paths}`
    );
    ok(
      fetchCalls.filter((call) => call.url.pathname === "/api/estimate").length === 1,
      `expected exactly one estimate per extent change, got: ${paths}`
    );
  });

  await test("moving the extent onto the 25 cm block re-derives everything, and moving off undoes it", async () => {
    const { sandbox, state } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(panelPart(sandbox, "coverage-lidar") === "LiDAR: 1 m", "expected the Wales level first");
    ok(!tickedSources(sandbox).includes("lidar_cardiff"), "expected the archive unticked off the block");

    const onTheBlock = cowbridgeCoverage();
    onTheBlock[4] = coverageEntry("lidar_cardiff", "full", "25 cm at this extent, flown 2011", true);
    state.entries = onTheBlock;
    setField(sandbox, "bbox", ST_FAGANS);
    await flush(10);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: 25 cm",
      `expected the highlight to follow the extent, got: ${panelPart(sandbox, "coverage-lidar")}`
    );
    ok(tickedSources(sandbox).includes("lidar_cardiff"), "expected the archive silently gained");

    state.entries = cowbridgeCoverage();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: 1 m",
      `expected it silently lost again, got: ${panelPart(sandbox, "coverage-lidar")}`
    );
    ok(!tickedSources(sandbox).includes("lidar_cardiff"), "expected the archive unticked again");
    ok(state.asked.length === 3, `expected one coverage call per extent, got ${state.asked.length}`);
    ok(state.asked[1] === ST_FAGANS, `expected the drawn extent asked about, got: ${state.asked[1]}`);
  });

  await test("a heavy source that is not yet cached gets one short bullet with its size", async () => {
    const { sandbox } = await coverageSandbox({
      // What /api/estimate reports for a cold cache: os_uprn prices its
      // one-time national download, everything else is per-run change.
      estimateSources: [
        { id: "osm", bytes_estimate: 1_200_000, seconds_estimate: 20 },
        { id: "os_uprn", bytes_estimate: 619_000_000, seconds_estimate: 300 },
      ],
    });
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    const bullets = panelBullets(sandbox);
    ok(
      bullets.join(" | ") === "Addresses: 619 MB first use",
      `expected exactly the one heavy bullet, got: ${JSON.stringify(bullets)}`
    );
    // The layer is available and selected, so the confirmation stands:
    // this bullet is a cost, not a refusal.
    ok(
      sandbox.document.getElementById("coverage-all").hidden === false,
      "a heavy download is not a missing layer"
    );
  });

  await test("a heavy source already cached gets no bullet at all", async () => {
    // os_uprn.estimate() returns 0 bytes once a complete cache answers for
    // it, which is how this page knows the 619 MB has already been paid
    // without asking a second question about it.
    const { sandbox } = await coverageSandbox({
      estimateSources: [
        { id: "osm", bytes_estimate: 1_200_000, seconds_estimate: 20 },
        { id: "os_uprn", bytes_estimate: 0, seconds_estimate: 1 },
      ],
    });
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    ok(panelBullets(sandbox).length === 0, `expected no bullets, got: ${JSON.stringify(panelBullets(sandbox))}`);
  });

  await test("the heavy bullet stays short even when its short name collides with another source's", async () => {
    // lidar_cardiff's own display_name is the real, 79-character one
    // (COVERAGE_SOURCE_REGISTRY's own comment says so on purpose), and
    // its short form, "LiDAR terrain", collides with lidar_wales, which
    // is never heavy and so never appears in this bullet list at all.
    // Before this fix, that collision made the disambiguated, full name
    // win here regardless, and the bullet reached 94 characters.
    const entries = cowbridgeCoverage();
    entries[4] = coverageEntry("lidar_cardiff", "full", "25 cm at this extent, flown 2011", true);
    const { sandbox } = await coverageSandbox({
      entries,
      estimateSources: [{ id: "lidar_cardiff", bytes_estimate: 84_000_000, seconds_estimate: 6 }],
    });
    setField(sandbox, "bbox", ST_FAGANS);
    setField(sandbox, "region", "Cardiff");
    setField(sandbox, "site", "St Fagans");
    await flush(20);
    const bullets = panelBullets(sandbox);
    ok(
      bullets.join(" | ") === "LiDAR terrain: 84 MB first use",
      `expected the short, uncollided name, got: ${JSON.stringify(bullets)}`
    );
    ok(
      bullets.every((bullet) => bullet.length <= 48),
      `the heavy bullet has grown back into the 94-character line, got: ${JSON.stringify(bullets)}`
    );
  });

  await test("unticking a heavy source clears its bullet at once, with no round trip to wait for", async () => {
    const { sandbox } = await coverageSandbox({
      estimateSources: [
        { id: "osm", bytes_estimate: 1_200_000, seconds_estimate: 20 },
        { id: "os_uprn", bytes_estimate: 619_000_000, seconds_estimate: 300 },
      ],
    });
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    ok(
      panelBullets(sandbox).join(" | ") === "Addresses: 619 MB first use",
      `expected the heavy bullet before unticking, got: ${JSON.stringify(panelBullets(sandbox))}`
    );
    const boxes = [...sandbox.document.querySelectorAll("#sources input")];
    const addressesBox = boxes.find((box) => box.value === "os_uprn");
    addressesBox.checked = false;
    sandbox.document.getElementById("sources").fire("change");
    // Deliberately no flush(): renderCoveragePanel is wired to fire
    // synchronously off the same "change" event, reading the checkbox
    // state directly, so the bullet must already be gone before the
    // refreshEstimate() round trip this same event also starts has any
    // chance to come back.
    ok(
      panelBullets(sandbox).length === 0,
      `expected the bullet cleared the instant the box was unticked, got: ${JSON.stringify(panelBullets(sandbox))}`
    );
  });

  await test("a failed coverage call falls back to selecting everything, and blocks nothing", async () => {
    const { sandbox } = await coverageSandbox({ status: 500 });
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    ok(
      tickedSources(sandbox).length === COVERAGE_SOURCE_REGISTRY.length,
      `expected the old always-select behaviour back, got: ${tickedSources(sandbox)}`
    );
    ok(panelText(sandbox) === "", `expected no panel drawn from an answer that never came: ${panelText(sandbox)}`);
    ok(
      sandbox.document.getElementById("download").disabled === false,
      "an aid that cannot be fetched must never become a gate"
    );
  });

  await test("one malformed entry in a coverage payload is skipped, the rest still renders", async () => {
    // Before this fix, every downstream reader reached straight for
    // entry.id with no guard, so a single `null` in an otherwise
    // well-formed array threw the moment renderSources touched it,
    // synchronously and outside the try/catch: the whole extent update
    // for this rectangle was abandoned, silently, with the checklist and
    // panel left however they were before.
    //
    // Two "none" entries, os_uprn and lidar_cardiff, are the load-bearing
    // part of this fixture: they must stay UNticked, which they could
    // only do if the good entries around the bad one are still read
    // individually rather than the whole payload falling back to
    // always-select. inspire's own entry is the malformed one; dropping
    // it leaves that one source with no answer of its own, which the
    // existing per-source fallback (renderSources' own comment) reads as
    // "assume covered" exactly as it would for a source /api/coverage
    // never mentions at all.
    const entries = [
      coverageEntry("osm", "full", "traced footprints and centrelines"),
      coverageEntry("os_uprn", "none", null, true),
      null,
      coverageEntry("lidar_wales", "full", "1 m at this extent"),
      coverageEntry("lidar_cardiff", "none", null, true),
    ];
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      tickedSources(sandbox).join(",") === "inspire,lidar_wales,osm",
      `expected the good "none" entries respected and the bad one defaulted, got: ${tickedSources(sandbox)}`
    );
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: 1 m",
      `expected the panel to still render from the good entries, got: ${JSON.stringify(
        panelPart(sandbox, "coverage-lidar")
      )}`
    );
  });

  await test("a coverage payload with nothing usable at all falls back to selecting everything", async () => {
    // The other half of "degrade rather than vanish": a payload that
    // survived the fetch but carries not one entry this page can read
    // (every one missing even a string id) is exactly as unusable as a
    // failed call, and gets the identical always-select fallback.
    const { sandbox } = await coverageSandbox({ entries: [null, "garbage", 42, {}] });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      tickedSources(sandbox).length === COVERAGE_SOURCE_REGISTRY.length,
      `expected the old always-select behaviour back, got: ${tickedSources(sandbox)}`
    );
    ok(panelText(sandbox) === "", `expected no panel drawn from a payload with nothing usable: ${panelText(sandbox)}`);
  });

  await test("the panel stays a panel: its whole text is short for a fully covered extent", async () => {
    // The guard against this quietly growing back into what it replaced.
    // The tier list it replaced printed one line per category, each
    // carrying a source name and a detail sentence, and ran to several
    // hundred characters over an ordinary extent. A future change that
    // reintroduces a paragraph fails here rather than in front of the
    // owner.
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    const text = panelText(sandbox);
    ok(text.length > 0, "expected the panel to have rendered at all");
    ok(text.length <= 120, `the panel has grown to ${text.length} characters: ${JSON.stringify(text)}`);
  });

  await test("the long per-source detail strings survive, inside Advanced", async () => {
    // "Nothing else" on the panel is not the same as thrown away: the
    // sentence each source writes about this extent is the reason someone
    // opens Advanced to override a tick.
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    const html = sandbox.document.getElementById("sources").innerHTML;
    ok(html.includes("1 m at this extent"), `expected the Wales detail sentence in the list, got: ${html}`);
    ok(html.includes("Not covered here"), `expected the uncovered row to say so, got: ${html}`);
    ok(
      !panelText(sandbox).includes("1 m at this extent"),
      `the detail sentence leaked back onto the panel: ${panelText(sandbox)}`
    );
  });

  await test("Advanced is a collapsed native disclosure holding the checkboxes and the tier list", () => {
    // Keyboard accessible with no JavaScript at all, which is why it is a
    // <details> rather than a div with a click handler, and closed on
    // arrival, which is the owner's "remove that from ui" without taking
    // the override away.
    const advanced = INDEX_HTML_MARKUP.indexOf("<details");
    ok(advanced !== -1, "expected a native <details> disclosure");
    const summary = INDEX_HTML_MARKUP.indexOf("<summary>Advanced</summary>");
    const sources = INDEX_HTML_MARKUP.indexOf('id="sources"');
    const tierList = INDEX_HTML_MARKUP.indexOf('id="tier-list"');
    const closed = INDEX_HTML_MARKUP.indexOf("</details>");
    ok(summary > advanced, "expected the summary inside the disclosure");
    ok(sources > summary && sources < closed, "expected the checkboxes inside it");
    ok(tierList > summary && tierList < closed, "expected the tier list inside it too, not deleted");
    ok(
      !/<details[^>]*\bopen\b/.test(INDEX_HTML_MARKUP),
      "expected it collapsed: the owner asked for these selections to be off the panel"
    );
    // And the panel's own three elements are outside it, or the highlight
    // would be behind the disclosure the brief exists to hide.
    ok(INDEX_HTML_MARKUP.indexOf('id="coverage-lidar"') < advanced, "expected the highlight outside Advanced");
    ok(INDEX_HTML_MARKUP.indexOf('id="coverage-all"') < advanced, "expected the confirmation outside Advanced");
    ok(INDEX_HTML_MARKUP.indexOf('id="coverage-notes"') < advanced, "expected the bullets outside Advanced");
  });

  // The three bodies below were not written here. They are what a real
  // mapgen server, with all eight sources registered, actually answered
  // for three real extents on 2026-08-12, copied verbatim. The panel
  // reads a level out of prose that lidar_wales.py and lidar_cardiff.py
  // own, so a fixture in this file's own words would prove that the panel
  // can parse this file rather than that it can parse them.
  const LIVE_COWBRIDGE = [
    { id: "osm", coverage: "full", heavy: false, detail: "traced footprints and centrelines, typically 1 to 5 m positional accuracy" },
    { id: "overture", coverage: "full", heavy: false, detail: "traced footprints and centrelines, typically 1 to 5 m positional accuracy" },
    { id: "elevation", coverage: "full", heavy: false, detail: "30 m (Copernicus GLO-30)" },
    { id: "os_open", coverage: "full", heavy: false, detail: "1:10,000 scale, generalized footprints (OS OpenMap Local)" },
    { id: "os_uprn", coverage: "full", heavy: true, detail: "one point per addressable location" },
    { id: "inspire", coverage: "full", heavy: false, detail: "indicative extents, not legal boundaries" },
    { id: "lidar_wales", coverage: "full", heavy: false, detail: "1 m at this extent" },
    { id: "lidar_cardiff", coverage: "none", heavy: true, detail: null },
  ];
  const LIVE_ST_FAGANS = LIVE_COWBRIDGE.map((entry) =>
    entry.id === "lidar_cardiff"
      ? { id: "lidar_cardiff", coverage: "full", heavy: true, detail: "25 cm at this extent, flown 2011" }
      : entry
  );
  const LIVE_COUNTY = LIVE_COWBRIDGE.map((entry) => {
    if (entry.id === "lidar_wales") {
      return {
        id: "lidar_wales",
        coverage: "partial",
        heavy: false,
        detail: "16 m at this extent (extents under about 4 x 4 km come back at 1 m)",
      };
    }
    if (entry.id === "lidar_cardiff") {
      // Re-recorded post-2026-08-12: the raster budget that produced the
      // appended "needs an extent under about 600 x 600 m here" clause
      // this string carried on the day this fixture was taken is gone
      // (the owner raised the cap to the coverage envelope's own pixel
      // count; see lidar_cardiff.py's own module docstring), so a real
      // server asked about this identical extent today answers with the
      // plain sentence, unconditionally, same as every other "partial"
      // answer this source ever gives.
      return {
        id: "lidar_cardiff",
        coverage: "partial",
        heavy: true,
        detail: "25 cm over part of this extent, flown 2011",
      };
    }
    if (entry.id === "inspire") return { ...entry, coverage: "partial" };
    return entry;
  });

  const LIVE_REGISTRY = [
    { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false },
    { id: "overture", display_name: "Overture Maps", licence: "ODbL", requires_api_key: false },
    { id: "elevation", display_name: "Elevation (OpenTopography)", licence: "Copernicus DEM", requires_api_key: true, api_key_config_field: "opentopography_api_key" },
    { id: "os_open", display_name: "OS Open map data (GB)", licence: "OGL", requires_api_key: false },
    { id: "os_uprn", display_name: "Addresses (OS Open UPRN, GB)", licence: "OGL", requires_api_key: false },
    { id: "inspire", display_name: "Property boundaries (INSPIRE)", licence: "OGL", requires_api_key: false },
    { id: "lidar_wales", display_name: "LiDAR terrain (Wales, 1 m)", licence: "OGL", requires_api_key: false },
    { id: "lidar_cardiff", display_name: "LiDAR terrain (St Fagans and St Georges-super-Ely, Cardiff, 25 cm, flown 2011)", licence: "OGL", requires_api_key: false },
  ];

  await test("the owner's own Cowbridge extent, against a real server's answer", async () => {
    // The survey that started this. Eight sources, one of which cannot
    // serve this ground, and what the owner should see is the level they
    // do get and no mention of a failure anywhere.
    const { sandbox } = await coverageSandbox({ entries: LIVE_COWBRIDGE, sources: LIVE_REGISTRY });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      panelText(sandbox) === "LiDAR: 1 m All other layers available",
      `unexpected panel: ${JSON.stringify(panelText(sandbox))}`
    );
    ok(
      tickedSources(sandbox).join(",") === "elevation,inspire,lidar_wales,os_open,os_uprn,osm,overture",
      `expected everything but the Cardiff archive selected, got: ${tickedSources(sandbox)}`
    );
  });

  await test("the same extent moved onto the 25 cm block, against a real server's answer", async () => {
    const { sandbox } = await coverageSandbox({ entries: LIVE_ST_FAGANS, sources: LIVE_REGISTRY });
    setField(sandbox, "bbox", ST_FAGANS);
    await flush(10);
    ok(
      panelText(sandbox) === "LiDAR: 25 cm All other layers available",
      `unexpected panel: ${JSON.stringify(panelText(sandbox))}`
    );
    ok(tickedSources(sandbox).length === 8, `expected all eight selected, got: ${tickedSources(sandbox)}`);
  });

  await test("a county-sized extent, against a real server's answer", async () => {
    // Both LiDAR sources answer "partial" here, and since the 2026-08-12
    // cap correction (lidar_cardiff.py's own module docstring) the finer
    // one, 25 cm, is what this partial extent actually gets: no budget
    // qualifier survives to say otherwise, so the finest DELIVERED level
    // wins the highlight outright and there is nothing left to redraw for.
    const { sandbox } = await coverageSandbox({ entries: LIVE_COUNTY, sources: LIVE_REGISTRY });
    setField(sandbox, "bbox", "-3.90,51.30,-3.10,51.75");
    await flush(10);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: 25 cm, part only",
      `unexpected highlight: ${JSON.stringify(panelPart(sandbox, "coverage-lidar"))}`
    );
    // inspire is "partial" here too (a non-LiDAR source), which item D's
    // fix now surfaces: the confirmation is withdrawn and a bullet names
    // it, the identical "part only" fragment the LiDAR highlight already
    // carries for the same fact.
    ok(
      sandbox.document.getElementById("coverage-all").hidden === true,
      "a non-LiDAR source with partial coverage must withdraw the confirmation"
    );
    ok(
      panelBullets(sandbox).includes("Property boundaries: part only"),
      `expected inspire's own partial coverage bulleted, got: ${JSON.stringify(panelBullets(sandbox))}`
    );
    ok(panelText(sandbox).length <= 120, `the panel has grown to ${panelText(sandbox).length} characters`);
  });

  await test("an override in Advanced is respected by the highlight, not talked over", async () => {
    // The highlight claims what this extent WILL GET. Unticking the only
    // covering LiDAR source in Advanced means it gets none, and a line
    // that went on quoting 1 m would be the same kind of untruth this
    // whole change exists to remove.
    const { sandbox } = await coverageSandbox();
    setField(sandbox, "bbox", COWBRIDGE);
    setField(sandbox, "region", "Vale of Glamorgan");
    setField(sandbox, "site", "Cowbridge");
    await flush(20);
    ok(panelPart(sandbox, "coverage-lidar") === "LiDAR: 1 m", "expected the level first");

    const wales = sandbox.document.querySelectorAll("#sources input").find((i) => i.value === "lidar_wales");
    wales.checked = false;
    sandbox.document.getElementById("sources").fire("change");
    await flush(20);
    ok(
      panelPart(sandbox, "coverage-lidar") === "LiDAR: none here",
      `expected the highlight to follow the override, got: ${JSON.stringify(
        panelPart(sandbox, "coverage-lidar")
      )}`
    );
    // And the override survives, because a tick is not an extent: only a
    // new rectangle re-derives the selection.
    ok(!tickedSources(sandbox).includes("lidar_wales"), "expected the override to hold");
  });

  await test("a covering source with nothing readable to say hides the highlight rather than claiming none", async () => {
    // "We cannot tell" and "there is none here" are different claims and
    // only one of them is true of a source that covers this ground but
    // whose detail an older server never sent.
    const entries = cowbridgeCoverage();
    entries[3] = coverageEntry("lidar_wales", "full", null);
    const { sandbox } = await coverageSandbox({ entries });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    ok(
      sandbox.document.getElementById("coverage-lidar").hidden === true,
      `expected no highlight at all, got: ${JSON.stringify(panelPart(sandbox, "coverage-lidar"))}`
    );
    ok(
      sandbox.document.getElementById("coverage-all").hidden === false,
      "the rest of the panel still has something true to say"
    );
  });

  await test("a hostile coverage payload is escaped before it reaches the panel", async () => {
    // Same reasoning as the tier list's own escaping test: this is the
    // server's own data, but the panel builds markup out of strings a
    // source composes, and nothing else here exercises that path.
    const entries = [
      coverageEntry("osm", "none", null),
      coverageEntry("lidar_wales", "full", "1 m at this extent"),
    ];
    const { sandbox } = await coverageSandbox({
      entries,
      sources: [
        {
          id: "osm",
          display_name: "<img src=x onerror=alert(1)>",
          licence: "ODbL",
          requires_api_key: false,
        },
        {
          id: "lidar_wales",
          display_name: "LiDAR terrain (Wales, 1 m)",
          licence: "OGL",
          requires_api_key: false,
        },
      ],
    });
    setField(sandbox, "bbox", COWBRIDGE);
    await flush(10);
    const notes = sandbox.document.getElementById("coverage-notes").innerHTML;
    ok(notes.includes("&lt;img"), `expected the tag escaped, got: ${notes}`);
    ok(!notes.includes("<img"), `expected no live tag in the bullet, got: ${notes}`);
  });

  await test("the OpenTopography API key is never included in a job's start payload", async () => {
    const SECRET = "sk-test-secret-should-never-leak";
    const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/sources") return jsonResponse(200, [ELEVATION_SOURCE_ENTRY]);
      if (url.pathname === "/api/extent") {
        return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
      }
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          warnings: [],
          folder: "C:\\Surveys\\R\\2026-08-02_S",
        });
      }
      if (url.pathname === "/api/jobs" && (options.method || "").toUpperCase() === "POST") {
        return jsonResponse(202, { id: "job1" });
      }
      if (url.pathname === "/api/jobs/job1") {
        return jsonResponse(200, {
          id: "job1",
          state: "done",
          events: [],
          error: null,
          result_root: "C:\\out",
        });
      }
      return null;
    });
    setApiKeyField(sandbox, "opentopography_api_key", SECRET);
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    await flush(10);
    fetchCalls.length = 0;
    sandbox.document.getElementById("download").fire("click");
    await flush(50);
    const jobCall = fetchCalls.find(
      (c) => c.url.pathname === "/api/jobs" && (c.options.method || "").toUpperCase() === "POST"
    );
    ok(jobCall, "expected a POST /api/jobs call");
    ok(!jobCall.options.body.includes(SECRET), `job start payload leaked the api key: ${jobCall.options.body}`);
    // Belt and suspenders: the whole job lifecycle, including the poller
    // rendering events into the visible log panel, must never surface it
    // either.
    await flush(50);
    const logText = sandbox.document.getElementById("log").children.map((l) => l.textContent).join(" ");
    ok(!logText.includes(SECRET), `the job log contained the api key: ${logText}`);
  });

  // =======================================================================
  // Task 18, item 2: place search as a typeahead. Request shape, result
  // handling (several matches, none, rate-limited, unreachable), keyboard
  // selection and Escape.
  // =======================================================================

  await test("typeahead sends a GET request with the typed query", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") return jsonResponse(200, []);
      return null;
    });
    fetchCalls.length = 0;
    typeIntoPlace(sandbox, "Barry");
    await flush(500);
    const call = fetchCalls.find((c) => c.url.pathname === "/api/geocode");
    ok(call, "expected a /api/geocode call");
    ok((call.options.method || "GET").toUpperCase() === "GET", "expected a GET request");
    ok(call.url.searchParams.get("q") === "Barry");
  });

  await test("typeahead renders every match's display name in the results list", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
          { display_name: "Barrie, Ontario", west: -79.72, south: 44.36, east: -79.63, north: 44.42 },
        ]);
      }
      return null;
    });
    typeIntoPlace(sandbox, "Bar");
    await flush(500);
    const list = sandbox.document.getElementById("place-results");
    ok(list.hidden === false, "expected the results list visible");
    ok(list.innerHTML.includes("Barry, Wales"), `expected Barry, Wales in: ${list.innerHTML}`);
    ok(list.innerHTML.includes("Barrie, Ontario"), `expected Barrie, Ontario in: ${list.innerHTML}`);
  });

  await test(
    "typeahead shows a message in the list, not the estimate panel, for the real server's 404 no-match response",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/geocode") return jsonResponse(404, { error: 'No match for "zzz"' });
        return null;
      });
      typeIntoPlace(sandbox, "zzz");
      await flush(500);
      const list = sandbox.document.getElementById("place-results");
      ok(list.hidden === false, "expected a message in the list rather than it staying empty");
      ok(/no match/i.test(list.innerHTML), `expected a no-match message, got: ${list.innerHTML}`);
      ok(
        sandbox.document.getElementById("estimate").className !== "estimate error",
        "a place with no matches must not surface as an estimate error"
      );
    }
  );

  await test(
    "typeahead also shows a message for a 200 with zero results (defensive: the real server always 404s that case instead)",
    async () => {
      // Review round 1: server.py:256 (if not results: return 404) means
      // a 200 with an empty array cannot happen against the real server
      // today, so runPlaceSearch's own `if (placeMatches.length) {...}
      // else {...}` branch for that shape had no test reaching it at
      // all: the test above only ever exercised the catch block's 404
      // handling. This exercises the try block's own empty-list branch
      // directly, so app.js keeps behaving correctly (a message, not an
      // empty open dropdown) even if that contract were ever to change.
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/geocode") return jsonResponse(200, []);
        return null;
      });
      typeIntoPlace(sandbox, "zzz");
      await flush(500);
      const list = sandbox.document.getElementById("place-results");
      ok(list.hidden === false, "expected a message rather than an empty, open dropdown");
      ok(/no match/i.test(list.innerHTML), `expected a no-match message, got: ${list.innerHTML}`);
    }
  );

  await test("typeahead shows a rate-limit message without disabling an existing valid estimate", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/extent") {
        return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
      }
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          warnings: [],
          folder: "C:\\out",
        });
      }
      if (url.pathname === "/api/geocode") {
        return jsonResponse(429, { error: "Too many geocoding requests are already waiting." });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    await flush(10);
    ok(sandbox.document.getElementById("download").disabled === false, "expected a valid estimate first");

    typeIntoPlace(sandbox, "Barry");
    await flush(500);
    const list = sandbox.document.getElementById("place-results");
    ok(/too many/i.test(list.innerHTML), `expected a rate-limit message, got: ${list.innerHTML}`);
    ok(
      sandbox.document.getElementById("download").disabled === false,
      "a place-search hiccup must not disable an otherwise-valid download"
    );
  });

  await test("Enter selects the top result when none has been arrow-highlighted", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
          { display_name: "Barrie, Ontario", west: -79.72, south: 44.36, east: -79.63, north: 44.42 },
        ]);
      }
      return null;
    });
    typeIntoPlace(sandbox, "Bar");
    await flush(500);
    sandbox.document.getElementById("place").fire("keydown", { key: "Enter" });
    const bboxValue = sandbox.document.getElementById("bbox").value;
    ok(bboxValue === "-3.31,51.38,-3.25,51.43", `expected the first match's bbox, got: ${bboxValue}`);
    ok(sandbox.document.getElementById("place-results").hidden === true, "expected the list to close on selection");
    ok(sandbox.document.getElementById("place").value === "Barry, Wales", "expected the field to show the chosen name");
  });

  // =======================================================================
  // Task 19, item 1: choosing a typeahead result fills region/site from
  // that result's own address, without waiting on a second reverse lookup
  // at the bbox centroid.
  // =======================================================================

  await test("choosing a typeahead result fills region and site from that result directly", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          {
            display_name: "Barry Island, Barry, Vale of Glamorgan, Wales, United Kingdom",
            west: -3.29,
            south: 51.37,
            east: -3.25,
            north: 51.41,
            region: "Vale of Glamorgan",
            site: "Barry Island",
          },
        ]);
      }
      // suggestNames' own fallback reverse lookup must never be reached
      // here: both fields are already filled by the chosen result before
      // setBBox's debounce would otherwise fire it.
      if (url.pathname === "/api/reverse") {
        throw new Error("reverse should not be called when the pick already supplied region and site");
      }
      return null;
    });
    typeIntoPlace(sandbox, "Barry Island");
    await flush(500);
    sandbox.document.getElementById("place").fire("keydown", { key: "Enter" });
    ok(sandbox.document.getElementById("region").value === "Vale of Glamorgan");
    ok(sandbox.document.getElementById("site").value === "Barry Island");
    await flush(500); // past the suggest debounce, to prove it never fires the reverse call
    ok(!fetchCalls.some((c) => c.url.pathname === "/api/reverse"), "expected no reverse lookup");
  });

  await test("choosing a typeahead result does not overwrite region or site already typed", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          {
            display_name: "Barry Island, Barry, Vale of Glamorgan, Wales, United Kingdom",
            west: -3.29,
            south: 51.37,
            east: -3.25,
            north: 51.41,
            region: "Vale of Glamorgan",
            site: "Barry Island",
          },
        ]);
      }
      return null;
    });
    setField(sandbox, "site", "My Own Site Name");
    typeIntoPlace(sandbox, "Barry Island");
    await flush(500);
    sandbox.document.getElementById("place").fire("keydown", { key: "Enter" });
    ok(sandbox.document.getElementById("site").value === "My Own Site Name", "expected typed site preserved");
    ok(sandbox.document.getElementById("region").value === "Vale of Glamorgan", "expected the still-empty region filled");
  });

  await test(
    "choosing a typeahead result with no address falls back to the debounced reverse lookup",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/geocode") {
          return jsonResponse(200, [
            { display_name: "Somewhere Unnamed", west: -3.29, south: 51.37, east: -3.25, north: 51.41 },
          ]);
        }
        if (url.pathname === "/api/reverse") {
          return jsonResponse(200, { region: "Fallback Region", site: "Fallback Site" });
        }
        return null;
      });
      typeIntoPlace(sandbox, "Somewhere Unnamed");
      await flush(500);
      sandbox.document.getElementById("place").fire("keydown", { key: "Enter" });
      ok(sandbox.document.getElementById("region").value === "", "expected region still blank right after the pick");
      await flush(500); // past the suggest debounce
      ok(sandbox.document.getElementById("region").value === "Fallback Region");
      ok(sandbox.document.getElementById("site").value === "Fallback Site");
    }
  );

  await test("ArrowDown twice then Enter selects the second result", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
          { display_name: "Barrie, Ontario", west: -79.72, south: 44.36, east: -79.63, north: 44.42 },
        ]);
      }
      return null;
    });
    typeIntoPlace(sandbox, "Bar");
    await flush(500);
    const place = sandbox.document.getElementById("place");
    place.fire("keydown", { key: "ArrowDown" });
    place.fire("keydown", { key: "ArrowDown" });
    place.fire("keydown", { key: "Enter" });
    const bboxValue = sandbox.document.getElementById("bbox").value;
    ok(bboxValue === "-79.72,44.36,-79.63,44.42", `expected the second match's bbox, got: ${bboxValue}`);
  });

  await test("Escape closes the results list", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
        ]);
      }
      return null;
    });
    typeIntoPlace(sandbox, "Barry");
    await flush(500);
    ok(sandbox.document.getElementById("place-results").hidden === false, "expected results visible first");
    sandbox.document.getElementById("place").fire("keydown", { key: "Escape" });
    ok(sandbox.document.getElementById("place-results").hidden === true, "expected Escape to close the list");
  });

  await test("Escape cancels a pending debounced search, not just the visible list", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") return jsonResponse(200, []);
      return null;
    });
    fetchCalls.length = 0;
    typeIntoPlace(sandbox, "Barry"); // debounce timer pending, nothing fetched yet
    sandbox.document.getElementById("place").fire("keydown", { key: "Escape" });
    await flush(600); // well past PLACE_DEBOUNCE_MS
    const geocodeCalls = fetchCalls.filter((c) => c.url.pathname === "/api/geocode");
    ok(geocodeCalls.length === 0, `expected Escape to cancel the pending search, got ${geocodeCalls.length} call(s)`);
  });

  await test("clicking away (blur) closes the results list after a short delay", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          { display_name: "Barry, Wales", west: -3.31, south: 51.38, east: -3.25, north: 51.43 },
        ]);
      }
      return null;
    });
    typeIntoPlace(sandbox, "Barry");
    await flush(500);
    ok(sandbox.document.getElementById("place-results").hidden === false);
    sandbox.document.getElementById("place").fire("blur");
    ok(
      sandbox.document.getElementById("place-results").hidden === false,
      "expected the list still open immediately after blur, to give a click on a result time to register"
    );
    await flush(200);
    ok(sandbox.document.getElementById("place-results").hidden === true, "expected the list closed after the delay");
  });

  await test("a display name is HTML-escaped before being rendered", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/geocode") {
        return jsonResponse(200, [
          {
            display_name: "<script>alert(1)</script> & Sons",
            west: -3.31,
            south: 51.38,
            east: -3.25,
            north: 51.43,
          },
        ]);
      }
      return null;
    });
    typeIntoPlace(sandbox, "Sons");
    await flush(500);
    const html = sandbox.document.getElementById("place-results").innerHTML;
    ok(!html.includes("<script>"), `expected the raw tag to be escaped, got: ${html}`);
    ok(html.includes("&lt;script&gt;"), `expected an escaped form present, got: ${html}`);
  });

  // =======================================================================
  // Task 18, item 3, rebuilt by Task 36, item 2: the rubber-band draw
  // tool. Armed state, the press-drag-release sequence, the preview
  // rectangle's lifecycle, and Escape leaving any previous extent
  // untouched.
  //
  // The gesture is modelled as the four events a real browser and a real
  // Leaflet actually produce, in the order they produce them: mousedown,
  // mousemove, mouseup, and then the click the browser fires on whatever
  // ancestor the press and release have in common. That trailing click is
  // not an embellishment: it is the event that used to place a corner,
  // and it now arrives after the tool has already disarmed itself, which
  // is the whole reason app.js has anything to say about it.
  // =======================================================================

  // One committed gesture, in the order a browser fires it. Deliberately
  // NOT a helper that skips the trailing click: every real drag ends with
  // one, and a harness that quietly left it out would be more forgiving
  // than the page it is testing.
  function dragExtent(sandbox, from, to, { click = true } = {}) {
    sandbox.L._mapObject.fire("mousedown", { latlng: from });
    sandbox.L._mapObject.fire("mousemove", { latlng: to });
    sandbox.L._mapObject.fire("mouseup", { latlng: to });
    if (click) sandbox.L._mapObject.fire("click", { latlng: to });
  }

  await test("pressing Draw extent arms the button with a visible state", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    const button = sandbox.document.getElementById("draw");
    ok(button.className === "armed", `expected the armed class, got ${JSON.stringify(button.className)}`);
    ok(/drag/i.test(button.textContent), `expected the label to say to drag, got ${button.textContent}`);
  });

  await test("arming the draw tool switches map panning off, and disarming switches it back on", async () => {
    // Without this, Leaflet's own Drag handler answers the same mousedown
    // and the map pans under the cursor for the whole length of every
    // rectangle the owner tries to draw.
    const { sandbox } = await bootedSandbox();
    ok(sandbox.L._mapObject.dragging.enabled() === true, "expected panning on to begin with");
    sandbox.document.getElementById("draw").fire("click");
    ok(sandbox.L._mapObject.dragging.enabled() === false, "expected panning off while armed");
    dragExtent(sandbox, { lat: 51.4, lng: -3.3 }, { lat: 51.42, lng: -3.28 });
    ok(sandbox.L._mapObject.dragging.enabled() === true, "expected panning back on once the draw is over");
  });

  await test("Escape switches map panning back on too, not only the button's look", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.document.fire("keydown", { key: "Escape" });
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "a cancelled draw must not leave the map unpannable"
    );
  });

  await test("pressing, dragging and releasing on the map commits the extent and disarms", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    ok(sandbox.document.getElementById("draw").className === "armed", "expected still armed while held");
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.L._rectangles.some((r) => !r.removed), "expected a live preview rectangle while dragging");
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.document.getElementById("draw").className === "", "expected disarmed on the release");
    ok(sandbox.document.getElementById("draw").textContent === "Draw extent");
    ok(
      sandbox.document.getElementById("bbox").value === "-3.3,51.4,-3.28,51.42",
      `expected the committed bbox, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  await test("a press with no release commits nothing at all", async () => {
    // The press is one half of a gesture, not a corner that stands on its
    // own: nothing may be committed until the button comes back up.
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.document.getElementById("bbox").value === "", "expected nothing committed mid-drag");
    ok(sandbox.document.getElementById("draw").className === "armed", "expected the tool still armed mid-drag");
  });

  await test("moving the cursor before pressing draws no preview", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(
      sandbox.L._rectangles.length === 0,
      `expected no rectangle from a hover before the press, got ${sandbox.L._rectangles.length}`
    );
  });

  await test(
    "the preview rectangle is reused across mousemoves and removed once the drag completes",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.document.getElementById("draw").fire("click");
      sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
      sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.41, lng: -3.29 } });
      sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
      ok(
        sandbox.L._rectangles.length === 1,
        `expected exactly one rectangle created across two mousemoves, got ${sandbox.L._rectangles.length}`
      );
      ok(sandbox.L._rectangles[0].removed === false);
      sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.42, lng: -3.28 } });
      ok(sandbox.L._rectangles[0].removed === true, "expected the preview rectangle removed on commit");
      ok(sandbox.L._rectangles.length === 2, "expected a second, committed rectangle from setBBox");
      ok(sandbox.L._rectangles[1].removed === false, "expected the committed rectangle to remain");
    }
  );

  await test("Escape cancels an in-progress draw and leaves the previous extent untouched", async () => {
    const { sandbox } = await bootedSandbox();
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39"); // the committed rectangle: _rectangles[0]
    const before = sandbox.document.getElementById("bbox").value;
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.5, lng: -3.1 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.55, lng: -3.05 } }); // the preview: _rectangles[1]
    ok(sandbox.L._rectangles.length === 2, "expected a committed rectangle plus a live preview mid-drag");
    ok(sandbox.L._rectangles[0].removed === false, "expected the committed rectangle untouched before Escape");
    sandbox.document.fire("keydown", { key: "Escape" });
    ok(sandbox.document.getElementById("draw").className === "", "expected disarmed after Escape");
    ok(sandbox.document.getElementById("draw").textContent === "Draw extent");
    ok(
      sandbox.L._rectangles[0].removed === false,
      "expected the PREVIOUSLY COMMITTED rectangle to remain untouched by Escape"
    );
    ok(sandbox.L._rectangles[1].removed === true, "expected only the PREVIEW rectangle removed by Escape");
    ok(
      sandbox.document.getElementById("bbox").value === before,
      `expected the previous extent untouched, got ${sandbox.document.getElementById("bbox").value}`
    );
    // And a release after Escape is not a second chance to commit the
    // rectangle the owner just cancelled.
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.55, lng: -3.05 } });
    ok(
      sandbox.document.getElementById("bbox").value === before,
      "a release after Escape must commit nothing"
    );
  });

  await test("Escape without an active drawing is a harmless no-op", async () => {
    // The mock's #draw starts blank (it does not model index.html's real
    // static "Draw extent" text), so this checks that Escape changes
    // nothing about the button, not that it matches a specific label.
    const { sandbox } = await bootedSandbox();
    const before = {
      text: sandbox.document.getElementById("draw").textContent,
      className: sandbox.document.getElementById("draw").className,
    };
    sandbox.document.fire("keydown", { key: "Escape" });
    ok(
      sandbox.document.getElementById("draw").textContent === before.text,
      "expected Escape with nothing armed to leave the button's text alone"
    );
    ok(
      sandbox.document.getElementById("draw").className === before.className,
      "expected Escape with nothing armed to leave the button's class alone"
    );
  });

  await test("pressing Draw extent again mid-draw discards the stranded first corner", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.document.getElementById("draw").textContent === "Release to finish");
    sandbox.document.getElementById("draw").fire("click"); // pressed again mid-gesture
    ok(
      sandbox.document.getElementById("draw").textContent === "Drag a rectangle",
      "expected a fresh arm, waiting for a press again"
    );
    ok(sandbox.L._rectangles.every((r) => r.removed), "expected the stranded preview rectangle cleaned up");
    // Confirms the map is genuinely waiting for a fresh press, not
    // carrying a leftover firstCorner from before: a release on its own
    // must commit nothing.
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 52.0, lng: -4.0 } });
    ok(sandbox.document.getElementById("bbox").value === "", "expected nothing committed by a stray release");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 52.0, lng: -4.0 } });
    ok(sandbox.document.getElementById("draw").textContent === "Release to finish");
  });

  // =======================================================================
  // Task 36, item 3: Select viewport. One press captures what is on
  // screen; the capture then stays put through any amount of panning and
  // zooming, and a second press captures the new view.
  // =======================================================================

  await test("Select viewport captures exactly what the map is showing", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.L._mapObject._setBounds({ west: -3.25, south: 51.45, east: -3.15, north: 51.52 });
    sandbox.document.getElementById("viewport").fire("click");
    ok(
      sandbox.document.getElementById("bbox").value === "-3.25,51.45,-3.15,51.52",
      `expected the current view committed verbatim, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  await test("a captured viewport stays put when the map is panned and zoomed afterwards", async () => {
    // The subtlety the owner named: this is a snapshot, not a binding.
    // The stub fires the real move/moveend/zoomend events, so a version
    // wired to any of them would move the extent here.
    const { sandbox } = await bootedSandbox();
    sandbox.L._mapObject._setBounds({ west: -3.25, south: 51.45, east: -3.15, north: 51.52 });
    sandbox.document.getElementById("viewport").fire("click");
    const captured = sandbox.document.getElementById("bbox").value;
    const rectanglesAfterCapture = sandbox.L._rectangles.length;

    sandbox.L._mapObject._setBounds({ west: -4.00, south: 50.00, east: -3.00, north: 51.00 });
    sandbox.L._mapObject._setBounds({ west: -2.00, south: 52.00, east: -1.00, north: 53.00 });

    ok(
      sandbox.document.getElementById("bbox").value === captured,
      `expected the captured extent unmoved by panning, got ${sandbox.document.getElementById("bbox").value}`
    );
    ok(
      sandbox.L._rectangles.length === rectanglesAfterCapture,
      "expected no rectangle redrawn by a pan: setBBox must not have run again"
    );
  });

  await test("pressing Select viewport again captures the new view", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.L._mapObject._setBounds({ west: -3.25, south: 51.45, east: -3.15, north: 51.52 });
    sandbox.document.getElementById("viewport").fire("click");
    sandbox.L._mapObject._setBounds({ west: -2.5, south: 52.1, east: -2.4, north: 52.2 });
    sandbox.document.getElementById("viewport").fire("click");
    ok(
      sandbox.document.getElementById("bbox").value === "-2.5,52.1,-2.4,52.2",
      `expected the second capture, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  await test("Select viewport does not move the map to frame what is already on it", async () => {
    // fitBounds pads and snaps to a whole zoom level, so fitting to the
    // view that was just captured is the one thing that would reliably
    // change it.
    const { sandbox } = await bootedSandbox();
    let fitted = 0;
    sandbox.L._mapObject.fitBounds = () => {
      fitted += 1;
    };
    sandbox.document.getElementById("viewport").fire("click");
    ok(fitted === 0, "expected no fitBounds call from a viewport capture");
  });

  await test("Select viewport puts an armed draw tool away rather than fighting it", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    sandbox.document.getElementById("viewport").fire("click");
    ok(sandbox.document.getElementById("draw").className === "", "expected the draw tool disarmed");
    ok(sandbox.L._mapObject.dragging.enabled() === true, "expected panning switched back on");
    ok(
      sandbox.document.getElementById("bbox").value === "-3.4,51.3,-3,51.6",
      `expected the viewport committed, not the half-drawn rectangle, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  // =======================================================================
  // Review round 1: a zero-area extent (a drag that ends where it began,
  // or a degenerate pasted bbox) must be rejected at the point of entry,
  // leaving any previously committed extent untouched, rather than
  // silently committing a degenerate box that only the server notices.
  // =======================================================================

  await test(
    "a drag that ends where it started is rejected, leaving the previous extent untouched",
    async () => {
      const { sandbox } = await bootedSandbox();
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      const before = sandbox.document.getElementById("bbox").value;
      // Live rectangles, not every rectangle ever created: a drag draws a
      // preview while it is happening, and what matters is that nothing
      // of it is left on the map afterwards.
      const liveBefore = sandbox.L._rectangles.filter((r) => !r.removed).length;
      sandbox.document.getElementById("draw").fire("click");
      dragExtent(sandbox, { lat: 51.5, lng: -3.1 }, { lat: 51.5, lng: -3.1 }); // released on the press
      ok(
        sandbox.document.getElementById("bbox").value === before,
        `expected the previous extent untouched, got ${sandbox.document.getElementById("bbox").value}`
      );
      ok(sandbox.document.getElementById("draw").className === "", "expected disarmed, not stuck armed");
      ok(sandbox.document.getElementById("draw").textContent === "Draw extent");
      ok(
        sandbox.L._rectangles.filter((r) => !r.removed).length === liveBefore,
        "expected no new rectangle left on the map by a zero-size drag"
      );
    }
  );

  await test(
    "a zero-width drag (same longitude, different latitude) is also rejected",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.document.getElementById("draw").fire("click");
      dragExtent(sandbox, { lat: 51.4, lng: -3.3 }, { lat: 51.5, lng: -3.3 }); // same lng only
      ok(sandbox.document.getElementById("bbox").value === "", "expected no bbox committed");
      ok(sandbox.document.getElementById("draw").className === "");
    }
  );

  await test("a pasted zero-area bbox is rejected without ever calling setBBox", async () => {
    const { sandbox } = await bootedSandbox();
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39"); // one real rectangle committed
    const rectanglesAfterFirstPaste = sandbox.L._rectangles.length;
    ok(rectanglesAfterFirstPaste === 1, "expected exactly one committed rectangle so far");

    setField(sandbox, "bbox", "10,20,10,25"); // west === east: zero area
    ok(
      sandbox.document.getElementById("estimate").className === "estimate error",
      "expected an error state for a degenerate pasted bbox"
    );
    ok(sandbox.document.getElementById("download").disabled === true);
    // The real proof that the previous extent is untouched: setBBox is
    // the only thing that ever creates a new committed rectangle, so if
    // the degenerate paste had reached it, this count would have grown.
    ok(
      sandbox.L._rectangles.length === rectanglesAfterFirstPaste,
      "expected no new rectangle from the degenerate paste: setBBox must not have run"
    );

    // And the guard does not wedge anything: a subsequent valid paste
    // still works normally.
    setField(sandbox, "bbox", "-3.40,51.30,-3.30,51.40");
    ok(
      sandbox.L._rectangles.length === rectanglesAfterFirstPaste + 1,
      "expected a valid paste after a rejected one to still commit normally"
    );
  });

  // =======================================================================
  // Task 22: dark/light theme, applied to the page and resolved for
  // whatever the tile grid draws itself with.
  // =======================================================================

  await test("boot() applies a saved theme to the page immediately", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/config") return jsonResponse(200, { ...DEFAULT_CONFIG, theme: "dark" });
      return null;
    });
    ok(sandbox.document.getElementById("theme").value === "dark");
    ok(sandbox.document.documentElement.getAttribute("data-theme") === "dark");
  });

  await test("choosing a theme in Settings applies it at once and persists it", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox();
    fetchCalls.length = 0;
    setField(sandbox, "theme", "dark");
    await flush(10);
    ok(sandbox.document.documentElement.getAttribute("data-theme") === "dark");
    const putCall = fetchCalls.find((c) => (c.options.method || "").toUpperCase() === "PUT");
    ok(putCall, "expected the theme change to be persisted");
    ok(JSON.parse(putCall.options.body).theme === "dark");
  });

  await test("choosing 'match system' clears any explicit theme override", async () => {
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/config") return jsonResponse(200, { ...DEFAULT_CONFIG, theme: "dark" });
      return null;
    });
    ok(sandbox.document.documentElement.getAttribute("data-theme") === "dark");
    setField(sandbox, "theme", "auto");
    await flush(10);
    ok(
      sandbox.document.documentElement.getAttribute("data-theme") === null,
      "expected no explicit override left once 'auto' is chosen, so the CSS media query alone governs"
    );
  });

  await test("effectiveTheme falls back to the system preference when the setting is auto", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("theme").value = "auto";
    ok(sandbox.effectiveTheme() === "light", "expected light with no system dark preference set");
    sandbox.window._setPrefersDark(true);
    ok(sandbox.effectiveTheme() === "dark", "expected dark once the system preference says so");
  });

  await test("an explicit theme choice overrides the system preference", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.window._setPrefersDark(true);
    sandbox.document.getElementById("theme").value = "light";
    ok(
      sandbox.effectiveTheme() === "light",
      "expected the explicit choice to win over a dark system preference"
    );
  });

  await test(
    "changing the theme repaints the rectangles already on the map",
    async () => {
      // Review finding I3. repaintTileGridTheme's loop body had never run
      // in any test: V8 block coverage put it at count 0, and renaming
      // rect.setStyle inside it left all 151 checks green. In a browser
      // that rename is a TypeError, and because the theme handler calls
      // this BEFORE persistConfig, the symptom is not a crash the owner
      // would report: the theme visibly changes and then silently fails
      // to save, every time, once a grid is on the map.
      //
      // So this asserts both halves: the rectangles genuinely restyle,
      // and the setting genuinely reaches the server afterwards.
      const { fetchCalls, sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, {
            tiles: 2,
            rows: 1,
            cols: 2,
            extent_km: { width: 1, height: 1 },
            tile_grid: [
              { tile_id: "r00_c00", west: -3.3, south: 51.4, east: -3.29, north: 51.41 },
              { tile_id: "r00_c01", west: -3.29, south: 51.4, east: -3.28, north: 51.41 },
            ],
          });
        }
        if (url.pathname === "/api/config") return jsonResponse(200, DEFAULT_CONFIG);
        return null;
      });
      setField(sandbox, "bbox", "-3.30,51.40,-3.28,51.41");
      await flush(10);

      // The two tile rectangles are the ones created after setBBox's own,
      // and they must genuinely be on the map for this to be the real
      // path rather than a loop over nothing (the gap that hid the bug).
      const tiles = sandbox.L._rectangles.slice(-2);
      ok(tiles.length === 2, "expected two tile rectangles to have been drawn");
      // One of them is moved off "pending" first, so the repaint has to
      // read each rectangle's own CURRENT state rather than repainting
      // everything one colour: a loop that ignored tileState would pass a
      // grid where every tile happens to be pending.
      sandbox.paintTileStates(new Map([["r00_c01", "failed"]]));
      const lightPending = tiles[0].options.fillColor;
      const lightFailed = tiles[1].options.fillColor;
      ok(
        lightPending === "#9c9686" && lightFailed === "#8c3b2e",
        `expected the light palette first, got ${lightPending} and ${lightFailed}`
      );

      fetchCalls.length = 0;
      setField(sandbox, "theme", "dark");
      await flush(10);

      ok(
        tiles[0].options.fillColor === "#6b6656",
        `expected the pending tile repainted dark, got ${tiles[0].options.fillColor}`
      );
      ok(
        tiles[1].options.fillColor === "#e0685a",
        `expected the FAILED tile repainted dark in its own colour, got ${tiles[1].options.fillColor}`
      );
      const putCall = fetchCalls.find((c) => (c.options.method || "").toUpperCase() === "PUT");
      ok(putCall, "expected the theme to still be persisted after the repaint");
      ok(JSON.parse(putCall.options.body).theme === "dark");
    }
  );

  // =======================================================================
  // Task 22: the tile grid drawn on the map from /api/extent's and
  // /api/estimate's own tile_grid, never recomputed client-side.
  // =======================================================================

  await test(
    "drawing an extent renders one tile-grid rectangle per tile_grid entry, and shows the legend",
    async () => {
      const { sandbox } = await bootedSandbox((url) => {
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, {
            tiles: 2,
            rows: 1,
            cols: 2,
            extent_km: { width: 1, height: 1 },
            tile_grid: [
              { tile_id: "r00_c00", west: -3.3, south: 51.4, east: -3.29, north: 51.41 },
              { tile_id: "r00_c01", west: -3.29, south: 51.4, east: -3.28, north: 51.41 },
            ],
          });
        }
        return null;
      });
      ok(
        sandbox.document.getElementById("tile-legend").hidden === true,
        "expected no legend before any extent exists"
      );
      setField(sandbox, "bbox", "-3.30,51.40,-3.28,51.41");
      await flush(10);
      // One rectangle for the committed extent itself (setBBox's own),
      // plus one per tile_grid entry: the grid is drawn from the
      // server's own geometry, never recomputed from the bbox here.
      ok(
        sandbox.L._rectangles.length === 3,
        `expected 3 rectangles total, got ${sandbox.L._rectangles.length}`
      );
      ok(
        sandbox.document.getElementById("tile-legend").hidden === false,
        "expected the legend visible once a grid is drawn"
      );
      ok(/Not started/.test(sandbox.document.getElementById("tile-legend").innerHTML));
    }
  );

  await test("a changed extent redraws the tile grid instead of accumulating rectangles", async () => {
    let call = 0;
    const { sandbox } = await bootedSandbox((url) => {
      if (url.pathname === "/api/extent") {
        call += 1;
        const grid =
          call === 1
            ? [{ tile_id: "r00_c00", west: -3.3, south: 51.4, east: -3.29, north: 51.41 }]
            : [
                { tile_id: "r00_c00", west: -3.3, south: 51.4, east: -3.29, north: 51.41 },
                { tile_id: "r00_c01", west: -3.29, south: 51.4, east: -3.28, north: 51.41 },
              ];
        return jsonResponse(200, {
          tiles: grid.length,
          rows: 1,
          cols: grid.length,
          extent_km: { width: 1, height: 1 },
          tile_grid: grid,
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.30,51.40,-3.28,51.41");
    await flush(10);
    setField(sandbox, "bbox", "-3.35,51.40,-3.25,51.42");
    await flush(10);
    const unremoved = sandbox.L._rectangles.filter((r) => !r.removed);
    // 2 committed-extent rectangles were created (setBBox removes its
    // own previous one each time), plus the second grid's own 2 tiles;
    // only the second extent and the second grid's 2 tiles should
    // remain: the first grid's single tile must not still be sitting
    // underneath the new one.
    ok(
      sandbox.L._rectangles.length === 5,
      `expected 5 rectangles created in total, got ${sandbox.L._rectangles.length}`
    );
    ok(
      unremoved.length === 3,
      `expected 3 unremoved rectangles (1 extent + 2 grid tiles), got ${unremoved.length}`
    );
  });

  // --- classifyTiles: pure event-to-state classification, the logic that
  // decides what colour each rectangle above actually gets painted. -------

  await test("classifyTiles: an untouched tile stays pending while the job runs", async () => {
    const { sandbox } = await bootedSandbox();
    const result = sandbox.classifyTiles(["r00_c00"], ["osm"], [], true);
    ok(result.get("r00_c00") === "pending");
  });

  await test(
    "classifyTiles: a tile_done event marks a tile active, not yet done, while the job runs",
    async () => {
      const { sandbox } = await bootedSandbox();
      const result = sandbox.classifyTiles(
        ["r00_c00"],
        ["osm", "overture"],
        [{ event: "tile_done", tile_id: "r00_c00" }],
        true
      );
      ok(result.get("r00_c00") === "active", `expected active, got ${result.get("r00_c00")}`);
    }
  );

  await test("classifyTiles: Overture's first per-type event alone does not mark a tile done", async () => {
    // The brief's own caution: Overture emits per tile AND per type, so a
    // tile is not done when the first event for it arrives.
    const { sandbox } = await bootedSandbox();
    const result = sandbox.classifyTiles(
      ["r00_c00"],
      ["overture"],
      [{ event: "tile_done", tile_id: "r00_c00", overture_type: "water" }],
      true
    );
    ok(result.get("r00_c00") === "active", `expected active (not yet done), got ${result.get("r00_c00")}`);
  });

  // --- Task 23: Overture stopped being tiled, and its progress events
  // deliberately did not. These pin the browser half of that decision,
  // because the whole reason the events stayed per tile is what
  // classifyTiles does with them, and "we read the function and it looked
  // right" is not the same as running it. ---------------------------

  await test("classifyTiles: an Overture-only run still lights up the whole grid", async () => {
    // The exact event stream OvertureSource.fetch now produces: one
    // whole-extent download per type, and after each one lands, a
    // tile_done for EVERY tile in the plan carrying that type. Nothing is
    // emitted per tile per download any more, because there is only one
    // download per type.
    const { sandbox } = await bootedSandbox();
    const tileIds = ["r00_c00", "r00_c01", "r01_c00", "r01_c01"];
    const events = [];
    for (const overtureType of ["water", "building"]) {
      for (const tileId of tileIds) {
        events.push({ event: "tile_done", source: "overture", tile_id: tileId, overture_type: overtureType });
      }
    }

    const midRun = sandbox.classifyTiles(tileIds, ["overture"], events, true);
    for (const tileId of tileIds) {
      ok(
        midRun.get(tileId) === "active",
        `expected every tile active mid-run, got ${tileId} = ${midRun.get(tileId)}`
      );
    }

    events.push({ event: "source_done", source: "overture" });
    const finished = sandbox.classifyTiles(tileIds, ["overture"], events, true);
    for (const tileId of tileIds) {
      ok(
        finished.get(tileId) === "done",
        `expected every tile done once overture reported source_done, got ${tileId} = ${finished.get(tileId)}`
      );
    }
  });

  await test("classifyTiles: a whole-area tile_id would leave the grid entirely dead", async () => {
    // Why Overture did NOT adopt ElevationSource's tile_id="whole-area"
    // convention, which is the tidier-looking match for a source that no
    // longer tiles. state.has(event.tile_id) is false for it, so every
    // event is silently dropped and an Overture-only run, which the owner
    // can select on its own, shows a dead grid from first second to last.
    // Asserted rather than argued, so anyone tempted to "tidy this up"
    // later meets the consequence as a failing test.
    const { sandbox } = await bootedSandbox();
    const tileIds = ["r00_c00", "r00_c01"];
    const events = [
      { event: "tile_done", source: "overture", tile_id: "whole-area", overture_type: "water" },
      { event: "tile_done", source: "overture", tile_id: "whole-area", overture_type: "building" },
    ];
    const result = sandbox.classifyTiles(tileIds, ["overture"], events, true);
    for (const tileId of tileIds) {
      ok(
        result.get(tileId) === "pending",
        `whole-area events should be ignored entirely, got ${tileId} = ${result.get(tileId)}`
      );
    }
  });

  await test("classifyTiles: a resumed Overture run lights the grid from tile_skipped alone", async () => {
    // On a resume every type is already on disk, so fetch emits
    // tile_skipped rather than tile_done and never runs the CLI at all.
    // The grid must still fill in, or a resume looks like a hung job.
    const { sandbox } = await bootedSandbox();
    const tileIds = ["r00_c00", "r00_c01"];
    const events = tileIds.map((tileId) => ({
      event: "tile_skipped",
      source: "overture",
      tile_id: tileId,
      overture_type: "water",
    }));
    events.push({ event: "source_done", source: "overture" });
    const result = sandbox.classifyTiles(tileIds, ["overture"], events, true);
    for (const tileId of tileIds) {
      ok(result.get(tileId) === "done", `expected done, got ${tileId} = ${result.get(tileId)}`);
    }
  });

  await test("classifyTiles: a tile is done once every selected source reports source_done", async () => {
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_done", tile_id: "r00_c00" },
      { event: "source_done", source: "osm" },
      { event: "tile_done", tile_id: "r00_c00", overture_type: "water" },
      { event: "source_done", source: "overture" },
    ];
    const result = sandbox.classifyTiles(["r00_c00"], ["osm", "overture"], events, true);
    ok(result.get("r00_c00") === "done", `expected done, got ${result.get("r00_c00")}`);
  });

  // =======================================================================
  // Task 36, item 6: tiles go green one at a time, as each one is
  // finished, rather than all at once at the end of the run. A tile is
  // done when every SELECTED SOURCE has finished with THAT TILE, not when
  // every source has finished with everything.
  // =======================================================================

  await test("a tile its only layer has finished is green while the rest of the run goes on", async () => {
    // The owner's own report: "the tiles turn orange one by one, great
    // showing that its in progress. but they dont individual turn green
    // either, just at the end it goes green."
    const { sandbox } = await bootedSandbox();
    const result = sandbox.classifyTiles(
      ["r00_c00", "r00_c01", "r00_c02"],
      ["osm"],
      [
        { event: "tile_done", source: "osm", tile_id: "r00_c00" },
        { event: "tile_skipped", source: "osm", tile_id: "r00_c01" },
      ],
      true
    );
    ok(result.get("r00_c00") === "done", `a finished tile is done, got ${result.get("r00_c00")}`);
    ok(result.get("r00_c01") === "done", `a skipped tile is done too, got ${result.get("r00_c01")}`);
    ok(
      result.get("r00_c02") === "pending",
      `and the ones still to come are not, got ${result.get("r00_c02")}`
    );
  });

  await test("one of two layers finishing a tile is not enough to call it done", async () => {
    // The caution the old blunt rule existed to honour, and the thing
    // this must not lose: a tile short of a layer the owner asked for is
    // not a finished tile.
    const { sandbox } = await bootedSandbox();
    const result = sandbox.classifyTiles(
      ["r00_c00"],
      ["osm", "overture"],
      [{ event: "tile_done", source: "osm", tile_id: "r00_c00" }],
      true
    );
    ok(result.get("r00_c00") === "active", `expected active, got ${result.get("r00_c00")}`);
  });

  await test("all eight of Overture's types for a tile still do not finish it", async () => {
    // Overture reports per tile AND per type, and the browser is never
    // told how many types are in play, so no number of its own events
    // can be the last word on a tile. Eight is the default selection's
    // count, which is what the owner's real run had.
    const { sandbox } = await bootedSandbox();
    const types = ["building", "water", "land", "land_use", "segment", "connector", "place", "infrastructure"];
    const result = sandbox.classifyTiles(
      ["r00_c00"],
      ["overture"],
      types.map((overtureType) => ({
        event: "tile_done",
        source: "overture",
        tile_id: "r00_c00",
        overture_type: overtureType,
      })),
      true
    );
    ok(
      result.get("r00_c00") === "active",
      `expected active until source_done, got ${result.get("r00_c00")}`
    );
  });

  await test("an elevation-only run has nothing to say per tile until its layer lands", async () => {
    // ElevationSource reports one event for the whole extent, under
    // tile_id "whole-area", which is not a tile of the plan. There is no
    // partial per-tile signal to read and inventing one would be a grid
    // that moved because time passed. So the grid waits, and settles at
    // source_done, which is the moment every tile genuinely does have its
    // elevation.
    const { sandbox } = await bootedSandbox();
    const tileIds = ["r00_c00", "r00_c01"];
    const landing = { event: "tile_done", source: "elevation", tile_id: "whole-area" };
    const midRun = sandbox.classifyTiles(tileIds, ["elevation"], [landing], true);
    for (const tileId of tileIds) {
      ok(
        midRun.get(tileId) === "pending",
        `expected pending while the one download runs, got ${tileId} = ${midRun.get(tileId)}`
      );
    }
    const finished = sandbox.classifyTiles(
      tileIds,
      ["elevation"],
      [landing, { event: "source_done", source: "elevation" }],
      true
    );
    for (const tileId of tileIds) {
      ok(
        finished.get(tileId) === "done",
        `expected done at source_done, got ${tileId} = ${finished.get(tileId)}`
      );
    }
  });

  await test("a resume greens the tiles already on disk in its first seconds", async () => {
    // The shape the owner actually meets: Stop, then Download again over
    // the same extent. package.py emits tile_skipped per source for every
    // tile its state.json already records as ok, BEFORE that source's
    // fetch is called, and those events are unqualified even for Overture
    // because they come from state.json rather than from a type's
    // download. So a tile already on disk for every layer is done at
    // once, with no source_done anywhere in the stream yet.
    const { sandbox } = await bootedSandbox();
    const events = [];
    for (const source of ["osm", "overture"]) {
      events.push({ event: "tile_skipped", source, tile_id: "r00_c00" });
    }
    // r00_c01 is one of the tiles the stop caught: only OpenStreetMap's
    // turn has come round to it so far.
    events.push({ event: "tile_skipped", source: "osm", tile_id: "r00_c01" });
    const result = sandbox.classifyTiles(["r00_c00", "r00_c01"], ["osm", "overture"], events, true);
    ok(
      result.get("r00_c00") === "done",
      `a tile every layer already has is done, got ${result.get("r00_c00")}`
    );
    ok(
      result.get("r00_c01") === "active",
      `a tile still short of a layer is not, got ${result.get("r00_c01")}`
    );
  });

  await test("a layer that has finished has finished with every tile", async () => {
    // source_done is the whole-source form of the same claim, and it is
    // what makes a mixed run work: OpenStreetMap reports tile by tile,
    // Overture cannot, so a tile becomes done the moment the layer that
    // could only speak for all of them says so.
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_done", source: "osm", tile_id: "r00_c00" },
      { event: "tile_done", source: "overture", tile_id: "r00_c00", overture_type: "building" },
      { event: "tile_done", source: "overture", tile_id: "r00_c01", overture_type: "building" },
      { event: "source_done", source: "overture" },
    ];
    const result = sandbox.classifyTiles(["r00_c00", "r00_c01"], ["osm", "overture"], events, true);
    ok(
      result.get("r00_c00") === "done",
      `both layers have finished this tile, got ${result.get("r00_c00")}`
    );
    ok(
      result.get("r00_c01") === "active",
      `OpenStreetMap has not reached this one, got ${result.get("r00_c01")}`
    );
  });

  await test("a tile a layer failed is finished with by that layer, and still red", async () => {
    // A failed tile will not be attempted again in this pass, so the
    // layer HAS finished with it. The ledger is what decides the colour,
    // and it still says failed, which is the point: red must not be
    // reachable only by a tile that is also unfinished.
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00"],
      ["osm"],
      [
        {
          event: "tile_failed",
          source: "osm",
          tile_id: "r00_c00",
          kind: "timeout",
          reason: "timed out.",
          retried: 1,
        },
      ],
      true
    );
    ok(
      summary.tileStates.get("r00_c00") === "failed",
      `expected failed, got ${summary.tileStates.get("r00_c00")}`
    );
  });

  await test("a tile whose failure the verify pass withdrew is finished, not left pending", async () => {
    // The other half of the line above, and the one that proves a
    // tile_failed really is an OUTCOME rather than merely a colour: the
    // verify pass is the only event that reports the reverse correction,
    // a tile recorded failed whose file was on disk after all, and it
    // arrives with no tile_done of its own to announce it. If a failure
    // did not count as that source finishing with the tile, this tile
    // would have nothing to its name at all and would sit pending on a
    // grid the server has already declared complete.
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00"],
      ["osm"],
      [
        {
          event: "tile_failed",
          source: "osm",
          tile_id: "r00_c00",
          kind: "no_output",
          reason: "no file was written.",
          retried: 0,
        },
        { event: "verify_done", phase: "after_fetch", failures: [] },
      ],
      true
    );
    ok(
      summary.tileStates.get("r00_c00") === "done",
      `expected done once the failure was withdrawn, got ${summary.tileStates.get("r00_c00")}`
    );
    ok(summary.tileFailures.length === 0, "expected no live reason left");
  });

  await test("a summary asked for with no layers selected finishes nothing", async () => {
    // [].every() is true, so without its own guard this would read every
    // tile of a run with no selected sources as done.
    const { sandbox } = await bootedSandbox();
    const result = sandbox.classifyTiles(
      ["r00_c00"],
      [],
      [{ event: "tile_done", source: "osm", tile_id: "r00_c00" }],
      true
    );
    ok(result.get("r00_c00") === "active", `expected active, got ${result.get("r00_c00")}`);
  });

  // The same rule as it actually reaches the map is checked further down,
  // beside the progress bar's own tests, because it needs the job
  // sandbox those tests build.

  await test(
    "classifyTiles: a failed tile is visibly distinct from one that is merely unfinished",
    async () => {
      const { sandbox } = await bootedSandbox();
      const events = [
        { event: "tile_done", tile_id: "r00_c00" },
        { event: "tile_failed", tile_id: "r00_c01" },
      ];
      const result = sandbox.classifyTiles(["r00_c00", "r00_c01", "r00_c02"], ["osm"], events, true);
      ok(result.get("r00_c00") === "active", "expected the in-progress tile to read active");
      ok(result.get("r00_c01") === "failed", "expected the failed tile to read failed");
      ok(result.get("r00_c02") === "pending", "expected the untouched tile to read pending");
      ok(
        new Set(result.values()).size === 3,
        "expected three genuinely distinct values, not two states doing duty for four"
      );
    }
  );

  // This check used to be "a failed tile stays failed even if a later
  // event for it arrives", and it fed exactly the stream below: a
  // tile_failed, then a tile_done for the same tile, commented in the
  // fixture as "stale/late, must not un-fail it". It was right when it
  // was written, because nothing in mapgen could fetch a tile twice.
  //
  // Task 30 can. A tile that fails is retried once at the end of the run,
  // and when the retry lands the stream is tile_failed, tile_retrying,
  // tile_done. Under the old rule that tile stayed red through a run that
  // finished complete, which is the one thing the grid's red must never
  // do: red has to mean a real attempt came up short, or the owner stops
  // believing it. So the reading of a later tile_done changed from
  // "stale" to "recovered", and it changed because the world under it did.
  //
  // What the old check was actually protecting is not gone, it is the
  // check below it: a tile_failed AFTER a tile_done still reads failed,
  // which is the ordinary shape of an OSM tile whose file turns out not
  // to be there and of every Overture type failure, since
  // _record_tile_outcomes runs after fetch() has emitted its own events.
  await test("classifyTiles: a tile retried and then landed is not a failure", async () => {
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_failed", source: "osm", tile_id: "r00_c00", kind: "timeout", reason: "timed out.", retried: 0 },
      { event: "tile_retrying", source: "osm", tile_id: "r00_c00", pass_number: 1, of: 1, kind: "timeout", reason: "timed out." },
      { event: "tile_done", source: "osm", tile_id: "r00_c00" },
      { event: "source_done", source: "osm" },
    ];
    const summary = sandbox.summariseJob(["r00_c00"], ["osm"], events, true);
    ok(
      summary.tileStates.get("r00_c00") === "done",
      `a recovered tile is done, got ${summary.tileStates.get("r00_c00")}`
    );
    ok(
      summary.tileFailures.length === 0,
      `expected no live reason for a tile that landed, got ${JSON.stringify(summary.tileFailures)}`
    );
  });

  await test("classifyTiles: a tile_failed arriving after a tile_done still reads failed", async () => {
    // The real ordering for an OSM tile whose file is not there: fetch()
    // emits tile_done per tile it thinks it got, and _record_tile_outcomes
    // reads the disk afterwards and disagrees.
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_done", source: "osm", tile_id: "r00_c00" },
      { event: "tile_failed", source: "osm", tile_id: "r00_c00", kind: "no_output", reason: "no file.", retried: 0 },
      { event: "source_done", source: "osm" },
    ];
    const summary = sandbox.summariseJob(["r00_c00"], ["osm"], events, false);
    ok(
      summary.tileStates.get("r00_c00") === "failed",
      `expected failed, got ${summary.tileStates.get("r00_c00")}`
    );
    ok(summary.tileFailures.length === 1, "expected the reason kept");
  });

  await test("classifyTiles: one layer finishing a tile does not clear another layer's failure", async () => {
    // The reason the ledger is keyed per source and not per tile. An
    // Overture tile_done for the same ground says nothing about whether
    // OpenStreetMap got it, and a tile short of one of two layers is
    // still a tile the owner needs to know about.
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_failed", source: "osm", tile_id: "r00_c00", kind: "timeout", reason: "timed out.", retried: 0 },
      { event: "tile_done", source: "overture", tile_id: "r00_c00", overture_type: "water" },
      { event: "source_done", source: "overture" },
      { event: "source_done", source: "osm" },
    ];
    const summary = sandbox.summariseJob(["r00_c00"], ["osm", "overture"], events, true);
    ok(
      summary.tileStates.get("r00_c00") === "failed",
      `expected failed, got ${summary.tileStates.get("r00_c00")}`
    );
    ok(summary.tileFailures.length === 1, "expected exactly the OpenStreetMap failure");
    ok(summary.tileFailures[0].source === "osm", summary.tileFailures[0].source);
  });

  await test(
    "classifyTiles: once the job stops running, a touched tile settles even without every source reporting done",
    async () => {
      // Models a stopped run: "osm" finished (source_done), "overture"
      // never got a turn at all, and the job has left "running".
      const { sandbox } = await bootedSandbox();
      const events = [
        { event: "tile_done", tile_id: "r00_c00" },
        { event: "source_done", source: "osm" },
      ];
      const result = sandbox.classifyTiles(["r00_c00"], ["osm", "overture"], events, false);
      ok(
        result.get("r00_c00") === "done",
        `expected done once the job has stopped running, got ${result.get("r00_c00")}`
      );
    }
  );

  // =======================================================================
  // Task 26: a tile too dense for one request is split into quarters and
  // recombined. The browser is told, and a subdivided tile still settles
  // to done: it is not a failure and must not be shown as one.
  // =======================================================================

  await test("classifyTiles: a tile_subdivided event does not disturb the grid", async () => {
    // The event carries a tile_id, so it reaches classifyTiles' own
    // tile_id branch, and it must fall straight through: subdivision is
    // something happening TO a tile that is still being fetched, not a
    // state of its own. Checked rather than assumed, because an unknown
    // event arriving with a KNOWN tile_id is exactly the shape that
    // would quietly repaint a rectangle if that branch were written
    // loosely.
    const { sandbox } = await bootedSandbox();
    const result = sandbox.classifyTiles(
      ["r00_c00", "r00_c01"],
      ["osm"],
      [{ event: "tile_subdivided", source: "osm", tile_id: "r00_c00", pieces: 4, depth: 1 }],
      true
    );
    ok(
      result.get("r00_c00") === "pending",
      `expected the subdivided tile still pending mid-fetch, got ${result.get("r00_c00")}`
    );
    ok(result.get("r00_c01") === "pending");
  });

  await test("classifyTiles: a subdivided tile still settles to done when it lands", async () => {
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_subdivided", source: "osm", tile_id: "r00_c00", pieces: 4, depth: 1 },
      // A quarter reports under its own id, which is not a tile of the
      // plan at all, so the grid must ignore it rather than growing an
      // entry for it.
      { event: "tile_skipped", source: "osm", tile_id: "r00_c00_q00" },
      { event: "tile_done", source: "osm", tile_id: "r00_c00" },
      { event: "source_done", source: "osm", outputs: ["Barry_2026-08-01.osm"] },
    ];
    const result = sandbox.classifyTiles(["r00_c00"], ["osm"], events, true);
    ok(
      result.get("r00_c00") === "done",
      `expected a subdivided tile to finish done, got ${result.get("r00_c00")}`
    );
    ok(result.size === 1, "a quarter id must not become a tile of its own");
  });

  await test("a tile_subdivided event is logged plainly, with its parent and piece count", async () => {
    // app.js formats no event by name: every one is logged as its name
    // plus its fields. That is what makes a new event safe to add from
    // the Python side, and it is worth pinning here, because the whole
    // point of this event is that the log says why a run has gone quiet
    // on one tile.
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          warnings: [],
          folder: "C:\\Surveys\\R\\2026-08-02_S",
        });
      }
      if (url.pathname === "/api/jobs" && (options.method || "").toUpperCase() === "POST") {
        return jsonResponse(202, { id: "job1" });
      }
      if (url.pathname === "/api/jobs/job1") {
        return jsonResponse(200, {
          id: "job1",
          state: "done",
          events: [
            { event: "tile_subdivided", source: "osm", tile_id: "r00_c00", pieces: 4, depth: 1 },
          ],
          error: null,
          result_root: "C:\\out",
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    await flush(10);
    sandbox.document.getElementById("download").fire("click");
    await flush(900);

    const logLines = sandbox.document.getElementById("log").children;
    const line = logLines.find((l) => l.textContent.startsWith("tile_subdivided"));
    ok(
      line,
      `expected a tile_subdivided log line, got: ${logLines.map((l) => l.textContent).join(" | ")}`
    );
    ok(
      line.textContent.includes("tile_id=r00_c00") && line.textContent.includes("pieces=4"),
      `expected the parent tile and the piece count in the line, got: ${line.textContent}`
    );
    ok(line.className !== "fail", "a split is not a failure and must not be styled as one");
  });

  // =======================================================================
  // Task 22: Stop keeps the data, so a stopped job is a normal, successful
  // outcome in the interface, never styled or worded like a failure.
  // =======================================================================

  await test("a stopped job is logged plainly, not styled as a failure", async () => {
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          warnings: [],
          folder: "C:\\Surveys\\R\\2026-08-02_S",
        });
      }
      if (url.pathname === "/api/jobs" && (options.method || "").toUpperCase() === "POST") {
        return jsonResponse(202, { id: "job1" });
      }
      if (url.pathname === "/api/jobs/job1") {
        return jsonResponse(200, {
          id: "job1",
          state: "stopped",
          events: [],
          error: null,
          result_root: "C:\\out",
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    await flush(10);
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("cancel").hidden === true,
      "expected the Stop button hidden once the job has stopped"
    );
    ok(
      sandbox.document.getElementById("download").disabled === false,
      "expected Download re-enabled once the job has stopped"
    );
    const logLines = sandbox.document.getElementById("log").children;
    const stoppedLine = logLines.find((l) => /stopped/i.test(l.textContent));
    ok(
      stoppedLine,
      `expected a stopped log line, got: ${logLines.map((l) => l.textContent).join(" | ")}`
    );
    ok(
      stoppedLine.className !== "fail",
      "expected a stopped job logged as a normal outcome, not styled like a failure"
    );
  });

  // =======================================================================
  // Task 27, part 1: how far through the run is, and how much longer.
  //
  // summariseJob is the one walk of the event stream that both the grid
  // and the bar read, and remainingLabel is the countdown's arithmetic
  // with the clock and the DOM taken out of it, so the awkward shapes (a
  // resume, a stall, an overrun) can be driven at the numbers instead of
  // by waiting for a real download to misbehave. The seconds used
  // throughout are the ones the estimate really produces for the owner's
  // Barry extent at the default tiling: 144s of OpenStreetMap, 18s of
  // Overture, 11s of elevation, 175s in total (see the constants in
  // sources/osm.py, overture.py and elevation.py).
  // =======================================================================

  const BARRY_SECONDS = { osm: 144, overture: 18, elevation: 11 };

  function tileIdsUpTo(count) {
    return Array.from({ length: count }, (unused, index) => `r00_c${String(index).padStart(2, "0")}`);
  }

  await test(
    "progress: Overture's many events per tile are one tile's worth of progress, not many",
    async () => {
      // The brief's own caution, at the numbers rather than at the tile
      // colours. Eight types across two of eight tiles is sixteen
      // events: counted per event that is 16/8 = two whole grids' worth
      // of progress, counted per (tile, source) it is the quarter of the
      // run it actually is. A partial tile set with every type is not a
      // contrived shape: package.py hands fetch() only the tiles still
      // pending, so a resumed run produces exactly this.
      const { sandbox } = await bootedSandbox();
      const tileIds = tileIdsUpTo(8);
      const events = [];
      for (const overtureType of ["water", "building", "land", "land_use", "infrastructure", "place", "segment", "connector"]) {
        for (const tileId of tileIds.slice(0, 2)) {
          events.push({ event: "tile_done", source: "overture", tile_id: tileId, overture_type: overtureType });
        }
      }
      const summary = sandbox.summariseJob(tileIds, ["overture"], events, true, BARRY_SECONDS);
      ok(
        Math.abs(summary.fractionDone - 0.25) < 1e-9,
        `expected two of eight tiles to read 25%, got ${summary.fractionDone}`
      );
    }
  );

  await test(
    "progress: elevation reports no tile of its own, and the bar still reaches 100%",
    async () => {
      // ElevationSource emits one event for the whole extent under
      // tile_id "whole-area", which is not a tile of the plan at all
      // (classifyTiles has its own test for why Overture did not copy
      // that convention). A denominator of tiles x sources therefore
      // caps a completed three-layer run at two thirds, and the owner
      // selects all three by default: the bar would finish every
      // ordinary download stuck at 67%.
      const { sandbox } = await bootedSandbox();
      const tileIds = tileIdsUpTo(2);
      const events = [
        ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
        { event: "source_done", source: "osm" },
        { event: "tile_done", source: "elevation", tile_id: "whole-area" },
        { event: "source_done", source: "elevation" },
      ];
      const summary = sandbox.summariseJob(tileIds, ["osm", "elevation"], events, true, BARRY_SECONDS);
      ok(
        Math.abs(summary.fractionDone - 1) < 1e-9,
        `expected a completed run to read 100%, got ${summary.fractionDone}`
      );
    }
  );

  await test(
    "progress: elevation shows no partial progress before its source_done, rather than a made-up share",
    async () => {
      // The counterpart to the test above: the whole-area event must not
      // be talked into meaning anything either. Elevation's share moves
      // at source_done and nowhere else, so the bar sits just short of
      // complete while the one download it has left is running.
      const { sandbox } = await bootedSandbox();
      const tileIds = tileIdsUpTo(2);
      const events = [
        ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
        { event: "source_done", source: "osm" },
        { event: "tile_done", source: "elevation", tile_id: "whole-area" },
      ];
      const summary = sandbox.summariseJob(tileIds, ["osm", "elevation"], events, true, BARRY_SECONDS);
      const expected = 144 / (144 + 11);
      ok(
        Math.abs(summary.fractionDone - expected) < 1e-9,
        `expected osm's own 144s share of 155s, got ${summary.fractionDone}`
      );
    }
  );

  await test(
    "progress: the sources are weighted by what the estimate says they cost, not one share each",
    async () => {
      // OpenStreetMap is 144s of a 162s two-layer run and Overture 18s.
      // Equal shares would call a finished OSM pass half the run when it
      // is nearly nine tenths of it, and the countdown reads off this
      // same fraction: it would have projected minutes onto a run with
      // seconds left.
      const { sandbox } = await bootedSandbox();
      const tileIds = tileIdsUpTo(4);
      const events = [
        ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
        { event: "source_done", source: "osm" },
      ];
      const weighted = sandbox.summariseJob(tileIds, ["osm", "overture"], events, true, BARRY_SECONDS);
      ok(
        Math.abs(weighted.fractionDone - 144 / 162) < 1e-9,
        `expected 144/162, got ${weighted.fractionDone}`
      );
      // With no estimate to weigh by (an older or partial response), it
      // falls back to equal shares rather than refusing to draw a bar.
      const unweighted = sandbox.summariseJob(tileIds, ["osm", "overture"], events, true, {});
      ok(
        Math.abs(unweighted.fractionDone - 0.5) < 1e-9,
        `expected equal shares without weights, got ${unweighted.fractionDone}`
      );
    }
  );

  await test(
    "progress: a stopped run reports the fraction it reached, while its tiles still settle",
    async () => {
      // The one place the tile states and the fractions deliberately
      // part company. classifyTiles settles every touched tile to done
      // once the job stops running, because the run is over and nothing
      // more is coming; the fraction must not, or a stop would always
      // read 100% and Task 22's whole distinction between stopping and
      // failing would be invisible on the bar.
      const { sandbox } = await bootedSandbox();
      const tileIds = tileIdsUpTo(4);
      const events = [
        ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
        { event: "source_done", source: "osm" },
      ];
      const summary = sandbox.summariseJob(tileIds, ["osm", "overture"], events, false, BARRY_SECONDS);
      for (const tileId of tileIds) {
        ok(summary.tileStates.get(tileId) === "done", `expected ${tileId} settled, got ${summary.tileStates.get(tileId)}`);
      }
      ok(
        summary.fractionDone < 0.9 && summary.fractionDone > 0.8,
        `expected a stopped run to report the 89% it reached, got ${summary.fractionDone}`
      );
    }
  );

  await test(
    "progress: tiles skipped on a resume count as done but never as measured work",
    async () => {
      // Stop, then Download again over the same extent, which is an
      // ordinary thing to do now that Stop keeps its data: every tile
      // already on disk reports tile_skipped in the first second. Those
      // are genuinely done, and they cost nothing, so the countdown must
      // be able to tell them apart from work this run actually did.
      const { sandbox } = await bootedSandbox();
      const tileIds = tileIdsUpTo(10);
      const events = [
        ...tileIds.slice(0, 8).map((tileId) => ({ event: "tile_skipped", source: "osm", tile_id: tileId })),
        { event: "tile_done", source: "osm", tile_id: tileIds[8] },
      ];
      const summary = sandbox.summariseJob(tileIds, ["osm"], events, true, BARRY_SECONDS);
      ok(Math.abs(summary.fractionDone - 0.9) < 1e-9, `expected 90% done, got ${summary.fractionDone}`);
      ok(Math.abs(summary.fractionSkipped - 0.8) < 1e-9, `expected 80% skipped, got ${summary.fractionSkipped}`);
      ok(Math.abs(summary.fractionFetched - 0.1) < 1e-9, `expected 10% fetched, got ${summary.fractionFetched}`);
    }
  );

  await test(
    "progress: a tile Overture skipped for one type and downloaded for another is fetched work",
    async () => {
      // A partially resumed Overture pass emits both events for the same
      // tile, one per type. Counting the skip would credit the run with
      // free work it actually paid for, and the projection is measured
      // off exactly that number.
      const { sandbox } = await bootedSandbox();
      const events = [
        { event: "tile_skipped", source: "overture", tile_id: "r00_c00", overture_type: "water" },
        { event: "tile_done", source: "overture", tile_id: "r00_c00", overture_type: "building" },
      ];
      const summary = sandbox.summariseJob(["r00_c00"], ["overture"], events, true, BARRY_SECONDS);
      ok(Math.abs(summary.fractionDone - 1) < 1e-9, `expected the tile fully reported, got ${summary.fractionDone}`);
      ok(summary.fractionSkipped === 0, `expected no skipped share, got ${summary.fractionSkipped}`);
      ok(Math.abs(summary.fractionFetched - 1) < 1e-9, `expected it counted as fetched, got ${summary.fractionFetched}`);
    }
  );

  await test("progress: a failed tile is work done, not work still outstanding", async () => {
    const { sandbox } = await bootedSandbox();
    const tileIds = tileIdsUpTo(4);
    const events = [
      { event: "tile_done", source: "osm", tile_id: tileIds[0] },
      { event: "tile_failed", source: "osm", tile_id: tileIds[1] },
    ];
    const summary = sandbox.summariseJob(tileIds, ["osm"], events, true, BARRY_SECONDS);
    ok(Math.abs(summary.fractionDone - 0.5) < 1e-9, `expected 50%, got ${summary.fractionDone}`);
  });

  await test("progress: one tile split twice is one tile, not two", async () => {
    // Review finding I5. This check was named "subdivisions are counted
    // from the events, once each", fed exactly the two events one
    // doubly-split plan tile produces, and asserted 2. It constructed the
    // wrong answer and pinned it: the countdown then told the owner "2
    // tiles were too dense" about one tile, and at MAX_SUBDIVISION_DEPTH
    // a single plan tile can produce five events and would have read "5
    // tiles were".
    //
    // The fixture's second id is r00_c00_q10, a real quarter id: quarters
    // are _q00, _q01, _q10 and _q11 (see geo.split_tile_into_quarters),
    // and the old fixture's r00_c00_q02 was an id that function cannot
    // produce.
    const { sandbox } = await bootedSandbox();
    const oneTileTwice = sandbox.summariseJob(
      ["r00_c00", "r00_c01"],
      ["osm"],
      [
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c00", pieces: 4, depth: 1 },
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c00_q10", pieces: 4, depth: 2 },
      ],
      true,
      BARRY_SECONDS
    );
    ok(
      oneTileTwice.subdivisions === 1,
      `one tile split twice is one tile, got ${oneTileTwice.subdivisions}`
    );

    // And two genuinely different plan tiles are still two, so this
    // counts rather than merely capping at one.
    const twoTiles = sandbox.summariseJob(
      ["r00_c00", "r00_c01"],
      ["osm"],
      [
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c00", pieces: 4, depth: 1 },
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c01", pieces: 4, depth: 1 },
      ],
      true,
      BARRY_SECONDS
    );
    ok(twoTiles.subdivisions === 2, `expected 2 tiles, got ${twoTiles.subdivisions}`);

    // The sentence the owner actually reads, through remainingLabel, is
    // what this finding was about: the count above only matters because
    // it is rendered as a count of tiles.
    const note = sandbox.remainingLabel({
      fractionDone: 0,
      fractionFetched: 0,
      fractionSkipped: 0,
      elapsedSeconds: 10,
      staticSeconds: 175,
      subdivisions: oneTileTwice.subdivisions,
    }).note;
    ok(/one tile was/i.test(note), `expected the singular reading, got: ${note}`);
    ok(!/2 tiles/.test(note), `expected no claim of two tiles, got: ${note}`);
  });

  // --- the countdown's own arithmetic ------------------------------------

  const RUNNING_RUN = {
    fractionDone: 0,
    fractionFetched: 0,
    fractionSkipped: 0,
    elapsedSeconds: 0,
    staticSeconds: 175,
    subdivisions: 0,
  };

  await test("countdown: before the crossover it counts down from the estimate", async () => {
    const { sandbox } = await bootedSandbox();
    const label = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.05,
      fractionFetched: 0.05,
      elapsedSeconds: 20,
    });
    ok(label.branch === "estimate", `expected the estimate branch, got ${label.branch}`);
    ok(label.text.includes("about 3 min"), `expected 155s to read as about 3 min, got ${label.text}`);
    // Task 38, item 5: "then the time '1 minute left' instead of
    // additional text after that time". The branch is still decided, and
    // still returned for anything that needs it; it is no longer read
    // out at the owner every 700ms.
    ok(label.text === "about 3 min left", `expected the time and nothing after it, got ${label.text}`);
  });

  await test("countdown: the crossover needs both enough work and enough clock", async () => {
    // 10% of the weighted work fetched AND 15 seconds of it. Either one
    // alone is a projection from noise: 9% is the brief's own "a
    // projection from 2% done is noise" one step up, and a fast-starting
    // run can pass 10% inside two seconds on skipped tiles alone.
    const { sandbox } = await bootedSandbox();
    const shortOfWork = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.09,
      fractionFetched: 0.09,
      elapsedSeconds: 40,
    });
    ok(shortOfWork.branch === "estimate", `9% fetched should not project, got ${shortOfWork.branch}`);
    const shortOfClock = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.5,
      fractionFetched: 0.5,
      elapsedSeconds: 14,
    });
    ok(shortOfClock.branch === "estimate", `14 seconds should not project, got ${shortOfClock.branch}`);
    const both = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.5,
      fractionFetched: 0.5,
      elapsedSeconds: 15,
    });
    ok(both.branch === "measured", `10% and 15s should project, got ${both.branch}`);
  });

  await test("countdown: past the crossover it projects from the run's own rate", async () => {
    const { sandbox } = await bootedSandbox();
    // Half the work in 60s, so half the work left is another 60s, even
    // though the static estimate said the whole run was 175s and would
    // therefore have claimed about 2 min left at this point.
    const label = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.5,
      fractionFetched: 0.5,
      elapsedSeconds: 60,
    });
    ok(label.branch === "measured", `expected the projection branch, got ${label.branch}`);
    ok(label.text.includes("about 1 min"), `expected about 1 min, got ${label.text}`);
    ok(label.text === "about 1 min left", `expected the time and nothing after it, got ${label.text}`);
    // The two branches are still told apart by everything except the
    // copy, which is what makes dropping the suffix a change of wording
    // rather than of behaviour: the same numbers on the estimate branch
    // would read differently.
    const fromEstimate = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.5,
      fractionFetched: 0.05,
      elapsedSeconds: 60,
    });
    ok(fromEstimate.branch === "estimate", `expected the other branch, got ${fromEstimate.branch}`);
    ok(fromEstimate.text !== label.text, "expected the two branches to still produce different times");
  });

  await test("countdown: a run that stalls reports a growing wait, not a frozen one", async () => {
    // The failure the brief warns a uniform event stream would never
    // catch. Nothing finishes between these two calls; only the clock
    // moves. A projection that measured the rate once and kept it would
    // sit at the same number while the run went nowhere.
    const { sandbox } = await bootedSandbox();
    const early = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.5,
      fractionFetched: 0.5,
      elapsedSeconds: 60,
    });
    const stalled = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.5,
      fractionFetched: 0.5,
      elapsedSeconds: 240,
    });
    ok(early.text.includes("about 1 min"), `expected about 1 min at 60s, got ${early.text}`);
    ok(stalled.text.includes("about 4 min"), `expected the wait to grow to about 4 min, got ${stalled.text}`);
  });

  await test(
    "countdown: a resumed run projects from what it fetched, not from what it skipped",
    async () => {
      // Half the run arrives as skipped tiles in the first two seconds.
      // Projecting from the total fraction would say two seconds left on
      // a run with five minutes to go; counting down the flat estimate
      // would say ten minutes on a run that only has half of it to do.
      const { sandbox } = await bootedSandbox();
      const label = sandbox.remainingLabel({
        fractionDone: 0.5,
        fractionFetched: 0,
        fractionSkipped: 0.5,
        elapsedSeconds: 2,
        staticSeconds: 600,
        subdivisions: 0,
      });
      ok(label.branch === "estimate", `expected the estimate branch with nothing fetched, got ${label.branch}`);
      ok(
        label.text.includes("about 5 min"),
        `expected half of a 10 min estimate, got ${label.text}`
      );
    }
  );

  await test("countdown: a run past its estimate says so rather than sitting at zero", async () => {
    const { sandbox } = await bootedSandbox();
    const label = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 0.05,
      fractionFetched: 0.05,
      elapsedSeconds: 300,
    });
    ok(label.branch === "overrun", `expected the overrun branch, got ${label.branch}`);
    ok(
      /taking longer/i.test(label.text) && !/\b0\b/.test(label.text),
      `expected an honest overrun line with no zero countdown, got ${label.text}`
    );
    // Task 38, item 5 shortened this one too, and the brief is explicit
    // that it had to survive in some short form: it is the only thing the
    // countdown can honestly say once the estimate has been passed.
    ok(label.text === "Taking longer than the estimate.", `got ${label.text}`);
    ok(!/left/.test(label.text), `an overrun has no time left to promise, got ${label.text}`);
  });

  await test("countdown: once every source has reported, it stops counting down", async () => {
    // The job stays "running" through the merge, the Urbano bridge step
    // and survey.json, none of which emit per-tile events. A countdown
    // here would be counting down to something it cannot see.
    const { sandbox } = await bootedSandbox();
    const label = sandbox.remainingLabel({
      ...RUNNING_RUN,
      fractionDone: 1,
      fractionFetched: 1,
      elapsedSeconds: 200,
    });
    ok(label.branch === "finishing", `expected the finishing branch, got ${label.branch}`);
    ok(/writing the package/i.test(label.text), `got ${label.text}`);
  });

  await test("countdown: a subdivision is said out loud, not absorbed silently", async () => {
    const { sandbox } = await bootedSandbox();
    const one = sandbox.remainingLabel({ ...RUNNING_RUN, elapsedSeconds: 10, subdivisions: 1 });
    ok(/one tile was/i.test(one.note), `expected a note about the split, got ${JSON.stringify(one.note)}`);
    ok(/longer than/i.test(one.note), `expected the note to say the run got longer, got ${one.note}`);
    const several = sandbox.remainingLabel({ ...RUNNING_RUN, elapsedSeconds: 10, subdivisions: 3 });
    ok(/3 tiles were/i.test(several.note), `expected a plural note, got ${several.note}`);
    const none = sandbox.remainingLabel({ ...RUNNING_RUN, elapsedSeconds: 10 });
    ok(none.note === "", `expected no note without a subdivision, got ${JSON.stringify(none.note)}`);
  });

  await test("countdown: with no estimate yet it says so rather than guessing", async () => {
    // Review finding N7: remainingLabel's "unknown" branch never
    // executed. It is reached whenever the snapshotted estimate is 0,
    // which is what a geometry-only estimate leaves behind, and the
    // honest answer there is that the number is not known yet rather
    // than a countdown from nothing.
    const { sandbox } = await bootedSandbox();
    const label = sandbox.remainingLabel({
      ...RUNNING_RUN,
      staticSeconds: 0,
      fractionDone: 0.05,
      fractionFetched: 0.05,
      elapsedSeconds: 5,
    });
    ok(label.branch === "unknown", `expected the unknown branch, got ${label.branch}`);
    ok(/working out/i.test(label.text), `got ${label.text}`);
    ok(!/\d/.test(label.text), `expected no number invented, got ${label.text}`);
  });

  await test("countdown: a non-positive remainder never reads as a negative time", async () => {
    // formatRemaining's own guard, also never executed. Reachable
    // through the measured branch when the projection lands at or below
    // zero on a run that is still going.
    const { sandbox } = await bootedSandbox();
    ok(sandbox.formatRemaining(0) === "less than a minute");
    ok(sandbox.formatRemaining(-30) === "less than a minute");
    ok(sandbox.formatRemaining(NaN) === "less than a minute");
  });

  await test("countdown: nothing is ever reported to the second", async () => {
    // The estimate under this is +/- 20% at best and one measured
    // Overture call came back at 28.48s against a 4.51s to 4.66s norm.
    // A countdown reading "3 min 12 s" would be claiming a precision
    // nothing in this tool has.
    const { sandbox } = await bootedSandbox();
    const samples = [1, 30, 59, 60, 95, 200, 599, 601, 700, 1800, 3600];
    for (const seconds of samples) {
      const text = sandbox.formatRemaining(seconds);
      ok(!/\bs\b|sec/i.test(text), `expected no seconds in "${text}" for ${seconds}s`);
    }
    ok(sandbox.formatRemaining(59) === "less than a minute", sandbox.formatRemaining(59));
    ok(sandbox.formatRemaining(95) === "about 2 min", sandbox.formatRemaining(95));
    // Past ten minutes it coarsens to five, because +/- 20% of twenty
    // minutes is four.
    ok(sandbox.formatRemaining(700) === "about 10 min", sandbox.formatRemaining(700));
    ok(sandbox.formatRemaining(1800) === "about 30 min", sandbox.formatRemaining(1800));
  });

  // --- the bar itself, driven through the real poll loop ------------------

  const TWO_SOURCES = [
    { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false, api_key_config_field: null },
    { id: "overture", display_name: "Overture", licence: "CDLA", requires_api_key: false, api_key_config_field: null },
  ];

  function gridFor(tileIds) {
    return tileIds.map((tileId, index) => ({
      tile_id: tileId,
      west: -3.3 + index * 0.01,
      south: 51.4,
      east: -3.29 + index * 0.01,
      north: 51.41,
    }));
  }

  // Boots a sandbox with a real estimate and a job whose status replies
  // are handed out one per poll, so a test can walk a run through as many
  // states as it needs without touching app.js's own timers.
  async function jobSandbox({ tileIds, polls, sources = TWO_SOURCES, sourceSeconds, cancelReply, startReply }) {
    let pollIndex = 0;
    const { sandbox, fetchCalls } = await bootedSandbox((url, options) => {
      const method = (options.method || "GET").toUpperCase();
      if (url.pathname === "/api/sources") return jsonResponse(200, sources);
      if (url.pathname === "/api/config" && method === "PUT") return jsonResponse(200, DEFAULT_CONFIG);
      // Answered at the job's OWN cancel path and nowhere else, on
      // purpose (review finding I4): a Stop posted anywhere but here
      // falls through to the stub's "unhandled fetch" error rather than
      // being quietly accepted, which is what makes the route itself part
      // of what the checks below pin. cancelReply lets a test choose what
      // the server says back, including refusing.
      if (url.pathname === "/api/jobs/job1/cancel" && method === "POST") {
        return cancelReply ? cancelReply() : jsonResponse(200, { cancelled: true });
      }
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: tileIds.length,
          rows: 1,
          cols: tileIds.length,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 175,
          warnings: [],
          folder: "C:\\out",
          tile_grid: gridFor(tileIds),
          sources: (sourceSeconds || [
            { id: "osm", seconds_estimate: 144 },
            { id: "overture", seconds_estimate: 18 },
          ]),
        });
      }
      if (url.pathname === "/api/jobs" && method === "POST") {
        // startReply lets a test have the server refuse the job, which is
        // how the Download handler's own catch gets exercised at all
        // (review finding N7).
        return startReply ? startReply() : jsonResponse(202, { id: "job1" });
      }
      if (url.pathname === "/api/jobs/job1") {
        // The last reply repeats once a test runs out of them, and a
        // test that supplies none at all (the slider tests, which never
        // start a job) gets a harmless running job rather than a
        // TypeError that would surface as an unrelated failure.
        const reply = polls[Math.min(pollIndex, polls.length - 1)] || { state: "running", events: [] };
        pollIndex += 1;
        if (reply.httpError) return jsonResponse(500, { error: "server fell over" });
        return jsonResponse(200, {
          id: "job1",
          error: null,
          result_root: "C:\\out",
          ...reply,
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    await flush(10);
    return { sandbox, fetchCalls };
  }

  await test("the bar appears when a download starts and fills from the events", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "running",
          events: tileIds.slice(0, 2).map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
        },
      ],
    });
    ok(
      sandbox.document.getElementById("progress").hidden === true,
      "expected no bar before a download has been started"
    );
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const progress = sandbox.document.getElementById("progress");
    ok(progress.hidden === false, "expected the bar visible once a job is running");
    ok(
      sandbox.document.getElementById("progress-fill").style.width === "50%",
      `expected a half-filled bar, got ${sandbox.document.getElementById("progress-fill").style.width}`
    );
    ok(
      progress.getAttribute("aria-valuenow") === "50",
      `expected a screen reader to be told the same 50, got ${progress.getAttribute("aria-valuenow")}`
    );
    const text = sandbox.document.getElementById("progress-text").textContent;
    ok(/left/.test(text), `expected a countdown, got ${text}`);
    // Task 38, item 5. The percentage is drawn, not written: the bar
    // immediately to the left of this copy is 140px of exactly that
    // number, and aria-valuenow above carries the same 50 to a screen
    // reader. Saying it a third time in words is what the owner asked to
    // have off this line.
    ok(!/%/.test(text), `expected no percentage repeated in the copy, got ${text}`);
  });

  await test("the bar reaches a complete, plainly finished state on a finished job", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("progress-fill").style.width === "100%",
      `expected a full bar, got ${sandbox.document.getElementById("progress-fill").style.width}`
    );
    ok(
      /finished/i.test(sandbox.document.getElementById("progress-text").textContent),
      sandbox.document.getElementById("progress-text").textContent
    );
    ok(
      sandbox.document.getElementById("progress").className === "progress",
      "a finished run is neither the stopped nor the failed styling"
    );
  });

  // Review finding I4: the Stop button, the browser half of Task 22's
  // headline feature, had no test at all. V8 coverage showed the handler
  // body never running, and changing its route to /api/jobs/<id>/NOTcancel
  // left all 151 checks green.

  await test("the Stop button posts to the running job's own cancel route", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox, fetchCalls } = await jobSandbox({
      tileIds,
      polls: [{ state: "running", events: [] }],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("cancel").hidden === false,
      "expected Stop visible while a job runs"
    );

    fetchCalls.length = 0;
    sandbox.document.getElementById("cancel").fire("click");
    await flush(10);

    const call = fetchCalls.find(
      (c) => (c.options.method || "").toUpperCase() === "POST"
    );
    ok(call, "expected the Stop click to send a POST");
    // The exact path, not a substring: this is the assertion the route
    // mutation has to get past.
    ok(
      call.url.pathname === "/api/jobs/job1/cancel",
      `expected /api/jobs/job1/cancel, got ${call.url.pathname}`
    );
    ok(call.url.searchParams.get("token") === DEFAULT_TOKEN, "the route is token gated");
  });

  await test("a Stop the server refuses says so instead of failing silently", async () => {
    // The owner's real case: the server has stopped, or the token is
    // stale, or the job id is unknown after a restart. api() throws for
    // all three, and with no try/catch that became an unhandled promise
    // rejection with nothing on screen and nothing in the log: the bar
    // kept counting down and the button looked dead.
    const rejections = [];
    const onRejection = (reason) => rejections.push(reason);
    process.on("unhandledRejection", onRejection);
    try {
      const tileIds = tileIdsUpTo(4);
      const { sandbox } = await jobSandbox({
        tileIds,
        polls: [{ state: "running", events: [] }],
        cancelReply: () => jsonResponse(404, { error: "Unknown job." }),
      });
      sandbox.document.getElementById("download").fire("click");
      await flush(900);
      const before = sandbox.document.getElementById("log").children.length;

      sandbox.document.getElementById("cancel").fire("click");
      await flush(50);

      const lines = sandbox.document.getElementById("log").children;
      ok(lines.length > before, "expected the refused Stop to say something in the log");
      const said = lines.slice(before).map((l) => l.textContent).join(" ");
      ok(/stop/i.test(said), `expected the message to be about stopping, got: ${said}`);
      ok(/unknown job/i.test(said), `expected the server's own reason, got: ${said}`);
      ok(
        lines.slice(before).some((l) => l.className === "fail"),
        "expected the failure styling, not a plain informational line"
      );
      ok(
        rejections.length === 0,
        `expected no unhandled rejection, got ${rejections.length}: ${rejections[0]}`
      );
    } finally {
      process.off("unhandledRejection", onRejection);
    }
  });

  await test("a Download the server refuses shows the reason instead of nothing", async () => {
    // Review finding N7: the Download handler's own catch never
    // executed, so neither a 409 from a busy server nor a 400 from
    // /api/jobs had ever been shown to reach the screen.
    const { sandbox } = await jobSandbox({
      tileIds: tileIdsUpTo(4),
      polls: [{ state: "running", events: [] }],
      startReply: () =>
        jsonResponse(409, {
          error: "A download is already running. Wait for it to finish or cancel it.",
        }),
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(20);
    const box = sandbox.document.getElementById("estimate");
    const shown = box.textContent || box.innerHTML;
    ok(/already running/i.test(shown), `expected the server's own reason, got: ${shown}`);
    ok(/error/.test(box.className), `expected the error styling, got ${box.className}`);
    ok(
      sandbox.document.getElementById("progress").hidden === true,
      "a job that never started must not raise a progress bar"
    );
  });

  await test("a stopped run settles the bar at what it reached, not at 100%", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      polls: [
        {
          state: "stopped",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const text = sandbox.document.getElementById("progress-text").textContent;
    const width = sandbox.document.getElementById("progress-fill").style.width;
    ok(width === "89%", `expected osm's 144 of 162 seconds, got ${width}`);
    ok(/stopped at 89%/i.test(text), `expected the reached fraction in the copy, got ${text}`);
    ok(
      sandbox.document.getElementById("progress").className === "progress stopped",
      `expected the stopped styling, got ${sandbox.document.getElementById("progress").className}`
    );
    ok(!/fail/i.test(text), `a stop is not a failure and must not be worded like one, got ${text}`);
  });

  await test("a failed run settles the bar into the failure state at the point it reached", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "failed",
          error: "Some tiles failed. See survey.json.",
          events: [
            { event: "tile_done", source: "osm", tile_id: tileIds[0] },
            { event: "tile_failed", source: "osm", tile_id: tileIds[1] },
            { event: "source_failed", source: "osm", error: "boom" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("progress").className === "progress failed",
      sandbox.document.getElementById("progress").className
    );
    ok(
      /failed at 50%/i.test(sandbox.document.getElementById("progress-text").textContent),
      sandbox.document.getElementById("progress-text").textContent
    );
  });

  await test("a subdivision mid-run is carried onto the bar, not only into the log", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "running",
          events: [
            { event: "tile_done", source: "osm", tile_id: tileIds[0] },
            { event: "tile_subdivided", source: "osm", tile_id: tileIds[1], pieces: 4, depth: 1 },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const note = sandbox.document.getElementById("progress-note");
    ok(note.hidden === false, "expected the note shown once a tile has been split");
    ok(/split/i.test(note.textContent), note.textContent);
  });

  await test("losing contact with the job stops the bar promising a countdown", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "running",
          events: [{ event: "tile_done", source: "osm", tile_id: tileIds[0] }],
        },
        { httpError: true },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(1600); // two polls: one good, one that fails
    const text = sandbox.document.getElementById("progress-text").textContent;
    ok(/lost contact/i.test(text), `expected the bar to say it has stopped updating, got ${text}`);
    ok(!/left/.test(text), `expected no countdown left promising anything, got ${text}`);
  });

  await test("a second download starts the bar over rather than continuing the last one", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(sandbox.document.getElementById("progress-fill").style.width === "100%");
    sandbox.document.getElementById("download").fire("click");
    await flush(10); // started, but no poll has come back yet
    ok(
      sandbox.document.getElementById("progress").hidden === true,
      "expected the previous run's full bar cleared, not left claiming this one is finished"
    );
  });

  await test("a run of one layer greens its grid rectangle by rectangle, not at the end", async () => {
    // Task 36, item 6, as it actually reaches the map: the pure function
    // tested further up is only half the claim, and the gap this file
    // exists to close is exactly the half where nothing ever painted a
    // rectangle. The job is still running when this is read.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "running",
          events: tileIds.slice(0, 2).map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const rects = gridRectangles(sandbox, tileIds);
    ok(
      rects[0].options.fillColor === "#2f5d4f" && rects[1].options.fillColor === "#2f5d4f",
      `expected the first two tiles green mid-run, got ${rects[0].options.fillColor} and ${rects[1].options.fillColor}`
    );
    ok(
      rects[2].options.fillColor === "#9c9686" && rects[3].options.fillColor === "#9c9686",
      `expected the untouched two still pending, got ${rects[2].options.fillColor} and ${rects[3].options.fillColor}`
    );
  });

  // =======================================================================
  // Task 36, item 5: the bar moved out of the form pane onto the strip
  // under the map, right of the tile legend, and gained one short line
  // saying what is happening now.
  //
  // The owner's constraint was a layout one as much as a feature: the
  // strip must not grow, and nothing else must move. A Node harness
  // cannot measure a box, so what is pinned here is the structure the
  // constraint rests on (one strip, carrying the legend's own padding and
  // border rather than a second set beside them, with the bar as a row
  // inside it) and the behaviour that would otherwise leave an empty band
  // under the map.
  // =======================================================================

  // The body of one rule, by exact selector, from the committed
  // stylesheet. Deliberately not a CSS parser and deliberately not a
  // substring search over the whole file: a check that "border-top
  // appears somewhere in styles.css" would pass against any of the
  // several rules that have one.
  function cssRule(selector) {
    const start = STYLES_CSS.indexOf(`\n${selector} {`);
    ok(start !== -1, `no rule for ${selector} in styles.css`);
    const open = STYLES_CSS.indexOf("{", start);
    const close = STYLES_CSS.indexOf("}", open);
    return STYLES_CSS.slice(open + 1, close);
  }

  await test("the bar is on the strip under the map, and the form pane no longer holds it", () => {
    const status = INDEX_HTML_MARKUP.indexOf('id="map-status"');
    const legend = INDEX_HTML_MARKUP.indexOf('id="tile-legend"');
    const bar = INDEX_HTML_MARKUP.indexOf('id="progress"');
    const tools = INDEX_HTML_MARKUP.indexOf('class="map-tools"');
    const formPane = INDEX_HTML_MARKUP.indexOf('class="form-pane"');
    ok(status !== -1 && legend !== -1 && bar !== -1, "expected all three elements in the markup");
    ok(status < legend && legend < bar, "expected the legend on the left of the bar, inside the strip");
    ok(bar < tools, "expected the whole strip above the map tools row");
    ok(bar < formPane, "expected the bar out of the form pane entirely, not copied out of it");
    ok(
      INDEX_HTML_MARKUP.split('id="progress"').length === 2,
      "expected exactly one progress bar in the document"
    );
  });

  await test("the strip is the legend's own strip, not a second one beside it", () => {
    // The whole no-taller argument rests on this: the padding, border and
    // background that used to draw the legend's band now draw the row
    // that holds both, so there is one band under the map and not two.
    const strip = cssRule(".map-status");
    const legend = cssRule(".tile-legend");
    ok(/border-top/.test(strip), `expected the strip to carry the top border: ${strip}`);
    ok(/padding/.test(strip), `expected the strip to carry the padding: ${strip}`);
    ok(!/border-top/.test(legend), `expected the legend to have given up its border: ${legend}`);
    ok(!/padding/.test(legend), `expected the legend to have given up its padding: ${legend}`);
    // And the bar is a row on that line rather than the stacked card it
    // was in the form pane, which is what makes one line of height enough.
    const bar = cssRule(".progress");
    ok(/flex-direction:\s*row/.test(bar), `expected the bar laid out as a row: ${bar}`);
    ok(/margin-left:\s*auto/.test(bar), `expected the bar pushed to the right: ${bar}`);
  });

  await test("the strip stays out of the way until there is something on it", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [{ state: "running", events: [] }],
    });
    // jobSandbox has already drawn a grid, so the legend is showing.
    ok(sandbox.document.getElementById("map-status").hidden === false, "expected the strip up with a legend on it");

    // No grid and no run: the map keeps the height back.
    setField(sandbox, "bbox", "10,20,10,25"); // rejected, clears the grid
    await flush(10);
    ok(
      sandbox.document.getElementById("tile-legend").hidden === true,
      "expected the legend gone with the grid"
    );
    ok(
      sandbox.document.getElementById("map-status").hidden === true,
      "expected the empty strip to take its band back"
    );
  });

  await test("the strip stays up for a bar with no legend beside it", async () => {
    // The two hide on their own account, so the strip must not read one
    // of them as the whole answer.
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("tile-legend").hidden = true;
    sandbox.document.getElementById("progress").hidden = false;
    sandbox.syncMapStatus();
    ok(
      sandbox.document.getElementById("map-status").hidden === false,
      "a bar with no legend still needs the strip"
    );
  });

  await test("the status line says which tile is being worked, and which package", async () => {
    // Task 38, item 5 rewrote this expectation. It used to read
    // "Downloading OpenStreetMap"; the owner asked for "which tile its
    // doing and what package its downloading".
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      polls: [
        {
          state: "running",
          events: [
            { event: "job_started", tiles: 4, root: "C:\\out" },
            { event: "tile_done", source: "osm", tile_id: tileIds[0] },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("progress-status").textContent === "osm r00_c00 · out",
      `expected the tile then the package, got ${sandbox.document.getElementById("progress-status").textContent}`
    );
  });

  await test("a layer with no tile of its own is named instead, and the package stays", async () => {
    // Overture downloads the whole extent per type and reports it across
    // the plan's tiles, one event per type, so naming a tile would say
    // the run is on one square when it is on all of them. The layer's own
    // display name is the true thing to say there, which is what the
    // brief asked for during a whole-extent layer.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      polls: [
        {
          state: "running",
          events: [
            { event: "job_started", tiles: 4, root: "C:\\out" },
            {
              event: "tile_done",
              source: "overture",
              tile_id: tileIds[0],
              overture_type: "building",
            },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("progress-status").textContent === "Downloading Overture · out",
      `expected the layer name, got ${sandbox.document.getElementById("progress-status").textContent}`
    );
  });

  await test("the package is named by its own folder, not the whole path to it", async () => {
    // <output root>/<region>/<date>_<site>: everything before the last
    // segment is the same for every run the owner makes, and this line
    // has one row of a shared strip to work in.
    const { sandbox } = await bootedSandbox();
    ok(
      sandbox.packageStem("C:\\Users\\Param\\Surveys\\south-wales\\2026-08-04_barry") ===
        "2026-08-04_barry",
      sandbox.packageStem("C:\\Users\\Param\\Surveys\\south-wales\\2026-08-04_barry")
    );
    // Composed server-side by pathlib, so it arrives with the separators
    // of whichever platform the server is on.
    ok(sandbox.packageStem("/home/param/surveys/wales/2026-08-04_barry") === "2026-08-04_barry");
    ok(sandbox.packageStem("C:\\out\\2026-08-04_barry\\") === "2026-08-04_barry", "a trailing slash");
    ok(sandbox.packageStem("") === "", "nothing known yet is nothing said");
    ok(sandbox.packageStem(null) === "", "and the same for a folder the server did not send");
  });

  await test("no estimate yet means no package name, and no stray separator", async () => {
    // A job cannot start without an estimate, but the line still has to
    // be composable from half of what it wants: the moment between the
    // job starting and its first event has a phase and, on a page whose
    // estimate never returned a folder, no package.
    const { sandbox } = await bootedSandbox();
    const line = sandbox.progressStatusLine({ kind: "checking", source: "" }, { result_root: null });
    ok(line === "Checking the files", `expected no dangling separator, got ${JSON.stringify(line)}`);
  });

  await test("the status line follows the run through its phases", async () => {
    const tileIds = tileIdsUpTo(4);
    const seen = [];
    const { sandbox } = await jobSandbox({
      tileIds,
      polls: [
        { state: "running", events: [{ event: "job_started", tiles: 4, root: "C:\\out" }] },
        {
          state: "running",
          events: [{ event: "job_started" }, { event: "tile_done", source: "overture", tile_id: tileIds[0] }],
        },
        {
          state: "running",
          events: [{ event: "job_started" }, { event: "source_done", source: "overture" }],
        },
        {
          state: "running",
          events: [{ event: "job_started" }, { event: "verify_done", phase: "after_fetch", failures: [] }],
        },
        {
          state: "running",
          events: [{ event: "job_started" }, { event: "bridge_started" }],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    for (let tick = 0; tick < 5; tick += 1) {
      await flush(800);
      seen.push(sandbox.document.getElementById("progress-status").textContent);
    }
    // The package's own name rides along on every one of them, since it
    // is what the run is building throughout (Task 38, item 5). The
    // second is a tile: that fixture's Overture event carries no
    // overture_type, which per Task 36's own rule is a claim about the
    // whole of that source's work for the tile, exactly like the skips a
    // resume emits.
    ok(seen[0] === "Starting · out", `got ${JSON.stringify(seen)}`);
    ok(seen[1] === "overture r00_c00 · out", `got ${JSON.stringify(seen)}`);
    ok(seen[2] === "Finished Overture · out", `got ${JSON.stringify(seen)}`);
    ok(seen[3] === "Checking the files · out", `got ${JSON.stringify(seen)}`);
    ok(seen[4] === "Writing the Urbano bridge · out", `got ${JSON.stringify(seen)}`);
  });

  await test("an event this page has never heard of leaves the status line alone", async () => {
    // The one place in app.js that reads an event by name. A new event
    // from the Python side must be able to arrive without this line
    // saying something untrue, and the log below still shows it in full.
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00"],
      ["osm"],
      [
        { event: "tile_done", source: "osm", tile_id: "r00_c00" },
        { event: "something_new_in_phase_2", source: "osm", detail: "whatever" },
      ],
      true
    );
    ok(summary.phase.kind === "downloading", `got ${JSON.stringify(summary.phase)}`);
    ok(summary.phase.tile === "r00_c00", `got ${JSON.stringify(summary.phase)}`);
    ok(sandbox.phaseLabel(summary.phase) === "osm r00_c00", sandbox.phaseLabel(summary.phase));
  });

  await test("a run that has ended leaves the status to the line that already says so", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("progress-status").textContent === "",
      `a finished run needs one label, not two: ${sandbox.document.getElementById("progress-status").textContent}`
    );
    ok(
      /finished/i.test(sandbox.document.getElementById("progress-text").textContent),
      sandbox.document.getElementById("progress-text").textContent
    );
  });

  await test("losing contact stops the status line naming something still happening", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        { state: "running", events: [{ event: "tile_done", source: "osm", tile_id: tileIds[0] }] },
        { httpError: true },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(800);
    ok(
      sandbox.document.getElementById("progress-status").textContent === "osm r00_c00 · out",
      sandbox.document.getElementById("progress-status").textContent
    );
    await flush(800);
    ok(
      sandbox.document.getElementById("progress-status").textContent === "",
      `expected the phase cleared once nothing is watching it: ${sandbox.document.getElementById("progress-status").textContent}`
    );
  });

  // =======================================================================
  // Task 27, part 2: tile size is a slider, and it says what each size
  // costs. Time is real and comes from the estimate; failure risk is not
  // shown at all, because it depends on how dense the data is on that
  // ground and nothing can know that before downloading it.
  // =======================================================================

  function dragTileSize(sandbox, value) {
    const el = sandbox.document.getElementById("tile-size");
    el.value = String(value);
    el.fire("input");
  }

  await test("the slider keeps the number field's own minimum", async () => {
    const { sandbox } = await bootedSandbox();
    const el = sandbox.document.getElementById("tile-size");
    ok(Number(el.min) === 500, `expected the 500 m minimum kept, got ${el.min}`);
    ok(Number(el.max) === 10000, `expected a 10000 m top, got ${el.max}`);
  });

  await test(
    "a saved tile size outside the slider's range widens the slider instead of rewriting the setting",
    async () => {
      // A slider must have a maximum where the number field had none, so
      // this is the one way the change could silently narrow what the
      // owner had already chosen: open Settings once with a saved 20000
      // and have it quietly become 10000, then be persisted.
      const { sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/config" && (!options.method || options.method === "GET")) {
          return jsonResponse(200, { ...DEFAULT_CONFIG, tile_size_m: 20000 });
        }
        return null;
      });
      const el = sandbox.document.getElementById("tile-size");
      ok(Number(el.max) >= 20000, `expected the slider widened to fit, got max ${el.max}`);
      ok(Number(el.value) === 20000, `expected the saved size kept, got ${el.value}`);
    }
  );

  await test("a saved tile size below the slider's minimum is kept too", async () => {
    const { sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/config" && (!options.method || options.method === "GET")) {
        return jsonResponse(200, { ...DEFAULT_CONFIG, tile_size_m: 300 });
      }
      return null;
    });
    const el = sandbox.document.getElementById("tile-size");
    ok(Number(el.min) <= 300, `expected the slider widened downwards, got min ${el.min}`);
    ok(Number(el.value) === 300, `expected the saved size kept, got ${el.value}`);
  });

  await test(
    "a saved tile size off the slider's grid is kept, not snapped and saved back",
    async () => {
      // Review finding I9. applyTileSizeBounds widened min and max for a
      // value outside the slider's range and never touched step, but a
      // range input snaps an assigned value to its step grid as well as
      // clamping it. Before Task 27 this control was <input type="number"
      // min="500" step="100">, which does not snap, and nothing calls
      // checkValidity, so a typed 1250 was read and saved; on the next
      // launch the slider rounded it to 1300 (ties go to the larger),
      // the estimate ran at 1300, and persistFieldSettings wrote 1300
      // back over config.json. tile_size_m is hashed into
      // naming.tiling_fingerprint, so an in-progress _work/<fingerprint>/
      // at 1250 was orphaned and every tile refetched for a setting the
      // owner never changed.
      //
      // Both existing bounds checks used 20000 and 300, which are on the
      // 100 m grid, so neither could have distinguished this.
      const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/config") {
          if (!options.method || options.method === "GET") {
            return jsonResponse(200, { ...DEFAULT_CONFIG, tile_size_m: 1250 });
          }
          return jsonResponse(200, DEFAULT_CONFIG);
        }
        if (url.pathname === "/api/extent") {
          return jsonResponse(200, { tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 } });
        }
        if (url.pathname === "/api/estimate") {
          return jsonResponse(200, {
            tiles: 1, rows: 1, cols: 1, extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000, seconds_estimate: 60, warnings: [], folder: "C:\\out",
            tile_grid: [], sources: [],
          });
        }
        return null;
      });
      const el = sandbox.document.getElementById("tile-size");
      ok(Number(el.value) === 1250, `expected the saved 1250 kept, got ${el.value}`);

      // And, the half that actually cost the owner their work directory:
      // the first successful estimate persists the field, and it must not
      // write a size back that nobody chose.
      fetchCalls.length = 0;
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      setField(sandbox, "region", "R");
      setField(sandbox, "site", "S");
      await flush(20);
      ok(
        sandbox.document.getElementById("download").disabled === false,
        "expected the estimate to have succeeded, so persistFieldSettings ran"
      );
      // persistConfig only sends what actually differs from the saved
      // state, so the right outcome here is no tile size written back AT
      // ALL. Asserted that way round rather than "a PUT carrying 1250":
      // a page that sends nothing and a page that sends the same number
      // are equally right, and only a page that sends a DIFFERENT number
      // is the defect. The snapped 1300 differs from the saved 1250, so
      // the old code did send one.
      const written = fetchCalls
        .filter((c) => (c.options.method || "").toUpperCase() === "PUT")
        .map((c) => JSON.parse(c.options.body).tile_size_m)
        .filter((size) => size !== undefined);
      ok(
        written.every((size) => size === 1250),
        `expected no tile size written back but 1250, got ${JSON.stringify(written)}: a ` +
          `different number here changes tiling_fingerprint and orphans any ` +
          `in-progress _work directory`
      );
    }
  );

  await test("a saved tile size ON the grid leaves the slider's own step alone", async () => {
    // The other half of the rule: the grid is only given up when a saved
    // setting needs it to be, so the ordinary launch still drags in
    // hundreds of metres.
    const { sandbox } = await bootedSandbox();
    const el = sandbox.document.getElementById("tile-size");
    ok(el.step === "100", `expected the 100 m grid kept, got step ${el.step}`);
    ok(Number(el.value) === 2000, `expected the saved default, got ${el.value}`);
  });

  await test("dragging the slider does not fire an estimate request per step", async () => {
    const { sandbox, fetchCalls } = await jobSandbox({ tileIds: tileIdsUpTo(4), polls: [] });
    fetchCalls.length = 0;
    for (let metres = 500; metres <= 4000; metres += 100) {
      dragTileSize(sandbox, metres);
      await flush(5);
    }
    await flush(600); // past the slider debounce
    const estimates = fetchCalls.filter((c) => c.url.pathname === "/api/estimate");
    ok(estimates.length === 1, `expected exactly one estimate for the whole drag, got ${estimates.length}`);
    ok(
      JSON.parse(estimates[0].options.body).tile_size_m === 4000,
      `expected the size the drag finished on, got ${estimates[0].options.body}`
    );
  });

  await test("releasing the slider does not fire a second estimate for the same size", async () => {
    // A click straight on the track fires input and change together. The
    // change listener refreshEstimate is already bound to would run, and
    // the pending debounce would then repeat it 400ms later.
    const { sandbox, fetchCalls } = await jobSandbox({ tileIds: tileIdsUpTo(4), polls: [] });
    fetchCalls.length = 0;
    dragTileSize(sandbox, 3000);
    setField(sandbox, "tile-size", "3000"); // the release
    await flush(600);
    const estimates = fetchCalls.filter((c) => c.url.pathname === "/api/estimate");
    ok(estimates.length === 1, `expected one estimate for one release, got ${estimates.length}`);
  });

  await test("the readout follows the handle without waiting for the debounce", async () => {
    const { sandbox } = await jobSandbox({ tileIds: tileIdsUpTo(4), polls: [] });
    dragTileSize(sandbox, 3500);
    ok(
      sandbox.document.getElementById("tile-size-readout").textContent === "3500 m",
      sandbox.document.getElementById("tile-size-readout").textContent
    );
  });

  await test("the time beside the slider is never the previous size's answer", async () => {
    // The whole point of showing a time per size is that it belongs to
    // that size. Mid-drag the estimate for the new position has not come
    // back, and relabelling the old one would be the exact lie this is
    // meant to avoid.
    const { sandbox } = await jobSandbox({ tileIds: tileIdsUpTo(4), polls: [] });
    const cost = sandbox.document.getElementById("tile-size-cost");
    ok(/about 3 min/.test(cost.textContent), `expected the settled estimate, got ${cost.textContent}`);
    dragTileSize(sandbox, 5000);
    ok(
      /working out/i.test(cost.textContent),
      `expected an honest "not yet known" mid-drag, got ${cost.textContent}`
    );
    await flush(600);
    ok(/about 3 min/.test(cost.textContent), `expected the new size's estimate, got ${cost.textContent}`);
    ok(/4 tiles/.test(cost.textContent), `expected the tile count alongside it, got ${cost.textContent}`);
  });

  await test(
    "an estimate that lands after the slider has moved on is not filed under the new size",
    async () => {
      // The slider can be moved while a request for the previous
      // position is still in flight, which a number field you commit
      // with Enter or a blur could not do nearly as easily. Reading the
      // field back when the reply arrives would label the old size's
      // tile count and time as this size's, which is the exact stale
      // number the "Working out the time at this size" line exists to
      // refuse to show.
      let resolveEstimate;
      const pending = new Promise((resolve) => {
        resolveEstimate = resolve;
      });
      let estimateCalls = 0;
      const { sandbox } = await bootedSandbox((url, options) => {
        const method = (options.method || "GET").toUpperCase();
        if (url.pathname === "/api/config" && method === "PUT") return jsonResponse(200, DEFAULT_CONFIG);
        if (url.pathname === "/api/estimate") {
          estimateCalls += 1;
          const body = {
            tiles: 9,
            rows: 3,
            cols: 3,
            extent_km: { width: 1, height: 1 },
            bytes_estimate: 1000,
            seconds_estimate: 175,
            warnings: [],
            folder: "C:\\out",
            sources: [{ id: "osm", seconds_estimate: 175 }],
          };
          // The first call (the settled 2000 m one) answers at once; the
          // one the drag to 4000 fires is held open.
          return estimateCalls === 1 ? jsonResponse(200, body) : pending;
        }
        return null;
      });
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      await flush(10);
      setField(sandbox, "region", "South Wales");
      setField(sandbox, "site", "Barry");
      await flush(10);

      dragTileSize(sandbox, 4000);
      await flush(600); // the 4000 m request is now in flight and held
      dragTileSize(sandbox, 6000); // the owner keeps going
      resolveEstimate(
        jsonResponse(200, {
          tiles: 9,
          rows: 3,
          cols: 3,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 175,
          warnings: [],
          folder: "C:\\out",
          sources: [{ id: "osm", seconds_estimate: 175 }],
        })
      );
      await flush(10);
      const cost = sandbox.document.getElementById("tile-size-cost").textContent;
      ok(
        /working out/i.test(cost),
        `expected the 4000 m answer not to be claimed for 6000 m, got ${cost}`
      );
    }
  );

  await test("the slider's sentence changes with position and states no risk figure", async () => {
    const { sandbox } = await bootedSandbox();
    const small = sandbox.tileSizeTrade(500);
    const middle = sandbox.tileSizeTrade(2000);
    const large = sandbox.tileSizeTrade(6000);
    ok(new Set([small, middle, large]).size === 3, "expected three genuinely different sentences");
    ok(/more requests/i.test(small), small);
    ok(/fewer requests/i.test(large), large);
    for (const sentence of [small, middle, large]) {
      ok(!sentence.includes("%"), `no fabricated percentage belongs here: ${sentence}`);
      ok(
        !/\b(low|medium|high|likely to fail|risk)\b/i.test(sentence),
        `no invented risk rating belongs here: ${sentence}`
      );
    }
  });

  await test("with no extent yet, the slider says so rather than showing a time", async () => {
    const { sandbox } = await bootedSandbox();
    ok(
      /draw or paste an extent/i.test(sandbox.document.getElementById("tile-size-cost").textContent),
      sandbox.document.getElementById("tile-size-cost").textContent
    );
  });

  // =======================================================================
  // Task 28: the elevation model select.
  //
  // The options come from GET /api/sources, not from index.html, so a
  // server that offers none must leave the page working exactly as it did
  // before this setting existed, and a saved model the server no longer
  // offers must not silently become an empty one on the next estimate.
  // Both are checked below, because both send a request the server
  // refuses with a 400 if this goes wrong.
  // =======================================================================

  const MODEL_SOURCE_ENTRY = {
    id: "elevation",
    display_name: "Elevation (OpenTopography)",
    licence: "Copernicus DEM",
    requires_api_key: true,
    api_key_config_field: "opentopography_api_key",
    demtype_choices: [
      { id: "COP30", label: "COP30, Copernicus surface model, 30 m (default)" },
      { id: "EU_DTM", label: "EU_DTM, bare earth terrain, 30 m, Europe" },
      { id: "COP90", label: "COP90, Copernicus surface model, 90 m" },
    ],
  };

  const MODEL_ESTIMATE = {
    tiles: 1,
    rows: 1,
    cols: 1,
    extent_km: { width: 1, height: 1 },
    bytes_estimate: 1000,
    seconds_estimate: 60,
    sources: [],
    warnings: [],
    folder: "C:\\Surveys\\South-Wales\\2026-08-04_Barry",
    tile_grid: [],
  };

  async function bootedWithModels(configOverrides = {}, sources = [MODEL_SOURCE_ENTRY]) {
    return bootedSandbox(async (url, options) => {
      if (url.pathname === "/api/config") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, ...configOverrides });
      }
      if (url.pathname === "/api/sources") return jsonResponse(200, sources);
      if (url.pathname === "/api/estimate") return jsonResponse(200, MODEL_ESTIMATE);
      return null;
    });
  }

  async function lastEstimateBody(sandbox, fetchCalls) {
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    const call = [...fetchCalls].reverse().find((c) => c.url.pathname === "/api/estimate");
    ok(call, "expected an /api/estimate call");
    return JSON.parse(call.options.body);
  }

  await test("the model select is filled from /api/sources, not from the markup", async () => {
    const { sandbox } = await bootedWithModels();
    const html = sandbox.document.getElementById("demtype").innerHTML;
    for (const choice of MODEL_SOURCE_ENTRY.demtype_choices) {
      ok(html.includes(`value="${choice.id}"`), `expected an option for ${choice.id}, got: ${html}`);
      ok(html.includes(choice.label), `expected the label for ${choice.id}, got: ${html}`);
    }
    ok(sandbox.document.getElementById("demtype-field").hidden === false);
  });

  await test("the saved model is what the select starts on", async () => {
    const { sandbox } = await bootedWithModels({ elevation_demtype: "EU_DTM" });
    const value = sandbox.document.getElementById("demtype").value;
    ok(value === "EU_DTM", `expected the saved model selected, got: ${value}`);
  });

  await test("a saved model the server no longer offers is kept, not silently dropped", async () => {
    // A select handed a value none of its options carry reports "" in a
    // real browser, and payload() would then send an empty model, which
    // the server refuses outright. Keeping it as an option of its own is
    // the same answer applyTileSizeBounds gives a tile size outside the
    // slider's range.
    const { sandbox } = await bootedWithModels({ elevation_demtype: "GEDTM30" });
    const html = sandbox.document.getElementById("demtype").innerHTML;
    ok(html.includes('value="GEDTM30"'), `expected the saved model kept as an option, got: ${html}`);
    ok(/saved earlier and kept/.test(html), `expected the page to say it is being kept, got: ${html}`);
    ok(sandbox.document.getElementById("demtype").value === "GEDTM30");
  });

  await test("a server that offers no models hides the control entirely", async () => {
    const { sandbox } = await bootedWithModels({}, DEFAULT_SOURCES);
    ok(sandbox.document.getElementById("demtype-field").hidden === true);
    ok(sandbox.document.getElementById("demtype").innerHTML === "");
    // And boot() got past it rather than throwing on an empty list: the
    // control being hidden is worth nothing if reaching that state broke
    // the rest of the page, which renders after this.
    ok(
      sandbox.document.getElementById("categories").innerHTML.includes("Buildings"),
      "boot() did not reach the categories, so it threw on the way there"
    );
  });

  await test("the chosen model travels in the estimate payload", async () => {
    const { sandbox, fetchCalls } = await bootedWithModels({ elevation_demtype: "EU_DTM" });
    const body = await lastEstimateBody(sandbox, fetchCalls);
    ok(
      body.elevation_demtype === "EU_DTM",
      `expected the model in the payload, got: ${JSON.stringify(body)}`
    );
  });

  await test("a page with no model offered sends no model key at all", async () => {
    // Absent means "the default, COP30" server-side; an empty string is a
    // malformed request that comes back 400. This is what an older server
    // produces, and it has to keep working.
    const { sandbox, fetchCalls } = await bootedWithModels({}, DEFAULT_SOURCES);
    const body = await lastEstimateBody(sandbox, fetchCalls);
    ok(!("elevation_demtype" in body), `expected no model key at all, got: ${JSON.stringify(body)}`);
  });

  await test("changing the model saves it and asks for a fresh estimate", async () => {
    const { sandbox, fetchCalls } = await bootedWithModels();
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    fetchCalls.length = 0;

    const select = sandbox.document.getElementById("demtype");
    select.value = "COP90";
    select.fire("change");
    await flush(10);

    const put = fetchCalls.find(
      (c) => c.url.pathname === "/api/config" && (c.options.method || "").toUpperCase() === "PUT"
    );
    ok(put, "expected the model to be saved immediately");
    ok(
      JSON.parse(put.options.body).elevation_demtype === "COP90",
      `unexpected PUT body: ${put.options.body}`
    );

    const estimate = fetchCalls.find((c) => c.url.pathname === "/api/estimate");
    ok(estimate, "expected a fresh estimate after changing the model");
    ok(JSON.parse(estimate.options.body).elevation_demtype === "COP90");
  });

  await test("model labels from the server are HTML-escaped", async () => {
    const { sandbox } = await bootedWithModels({}, [
      { ...MODEL_SOURCE_ENTRY, demtype_choices: [{ id: "COP30", label: "<b>COP30</b>" }] },
    ]);
    const html = sandbox.document.getElementById("demtype").innerHTML;
    ok(!html.includes("<b>COP30</b>"), `expected the label's tag escaped, got: ${html}`);
    ok(html.includes("&lt;b&gt;"), `expected an escaped label, got: ${html}`);
  });

  // =======================================================================
  // Task 28: the Browse button beside the output root.
  //
  // A stub that always hands back a path proves nothing about the three
  // outcomes that actually happen: a cancel, a picker that cannot run,
  // and a dialog left open until it was closed. All three are here, and
  // all three have to leave the field alone, because the text input is
  // the control and this button is only help beside it.
  // =======================================================================

  const OUTPUT_ROOT_CONFIG = { ...DEFAULT_CONFIG, output_root: "C:\\Surveys" };

  async function bootedWithPicker(dialogResponse) {
    return bootedSandbox(async (url, options) => {
      if (url.pathname === "/api/config" && (options.method || "GET").toUpperCase() === "GET") {
        return jsonResponse(200, OUTPUT_ROOT_CONFIG);
      }
      if (url.pathname === "/api/config") return jsonResponse(200, OUTPUT_ROOT_CONFIG);
      if (url.pathname === "/api/folder-dialog") return dialogResponse();
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 60,
          sources: [],
          warnings: [],
          folder: "C:\\Surveys\\South-Wales\\2026-08-04_Barry",
          tile_grid: [],
        });
      }
      return null;
    });
  }

  function clickBrowse(sandbox) {
    sandbox.document.getElementById("output-root-browse").fire("click");
  }

  function outputRoot(sandbox) {
    return sandbox.document.getElementById("output-root").value;
  }

  function outputRootNote(sandbox) {
    return sandbox.document.getElementById("output-root-note").textContent;
  }

  await test("Browse asks the server to open a dialog, starting where the field points", async () => {
    const { sandbox, fetchCalls } = await bootedWithPicker(() =>
      jsonResponse(200, { path: "D:\\NewSurveys" })
    );
    fetchCalls.length = 0;
    clickBrowse(sandbox);
    await flush(10);

    const call = fetchCalls.find((c) => c.url.pathname === "/api/folder-dialog");
    ok(call, "expected a POST /api/folder-dialog call");
    ok((call.options.method || "").toUpperCase() === "POST");
    ok(call.url.searchParams.get("token") === DEFAULT_TOKEN, "the route is token gated");
    ok(
      JSON.parse(call.options.body).initial === "C:\\Surveys",
      `expected the dialog to open where the field points, got: ${call.options.body}`
    );
  });

  await test("a chosen folder lands in the output root field", async () => {
    const { sandbox } = await bootedWithPicker(() => jsonResponse(200, { path: "D:\\NewSurveys" }));
    clickBrowse(sandbox);
    await flush(10);
    ok(outputRoot(sandbox) === "D:\\NewSurveys", `got: ${outputRoot(sandbox)}`);
    ok(outputRootNote(sandbox) === "", `expected no leftover note, got: ${outputRootNote(sandbox)}`);
  });

  await test("a Welsh folder path arrives intact", async () => {
    // The owner's ordinary input, not an edge case: procutil forces UTF-8
    // on every child precisely because a Welsh name killed a download
    // under cp1252 once already.
    const welsh = "C:\\Surveys\\Ynys M\u00f4n\\Rhoscolyn \u0177";
    const { sandbox } = await bootedWithPicker(() => jsonResponse(200, { path: welsh }));
    clickBrowse(sandbox);
    await flush(10);
    ok(outputRoot(sandbox) === welsh, `got: ${outputRoot(sandbox)}`);
  });

  await test("picking a folder is treated exactly like typing one", async () => {
    // Assigning .value fires nothing in a browser. Without a dispatched
    // change event the picked path would sit in the field, never reach an
    // estimate, and never be saved, so the next launch would come back
    // with the old one.
    const { sandbox, fetchCalls } = await bootedWithPicker(() =>
      jsonResponse(200, { path: "D:\\NewSurveys" })
    );
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    fetchCalls.length = 0;

    clickBrowse(sandbox);
    await flush(10);

    const estimate = [...fetchCalls].reverse().find((c) => c.url.pathname === "/api/estimate");
    ok(estimate, "expected a fresh estimate for the picked folder");
    ok(
      JSON.parse(estimate.options.body).output_root === "D:\\NewSurveys",
      `expected the picked folder in the estimate, got: ${estimate.options.body}`
    );
    const put = fetchCalls.find(
      (c) => c.url.pathname === "/api/config" && (c.options.method || "").toUpperCase() === "PUT"
    );
    ok(put, "expected the picked folder to be saved once the estimate accepted it");
    ok(JSON.parse(put.options.body).output_root === "D:\\NewSurveys");
  });

  await test("cancelling leaves the output root untouched and says so", async () => {
    const { sandbox } = await bootedWithPicker(() => jsonResponse(200, { path: null }));
    clickBrowse(sandbox);
    await flush(10);
    ok(outputRoot(sandbox) === "C:\\Surveys", `expected the field untouched, got: ${outputRoot(sandbox)}`);
    ok(/unchanged/i.test(outputRootNote(sandbox)), `got: ${outputRootNote(sandbox)}`);
  });

  await test("a picker that cannot run leaves the field alone and says to type it", async () => {
    const { sandbox } = await bootedWithPicker(() =>
      jsonResponse(503, { error: "The folder picker is not available on this machine. Type the folder path instead." })
    );
    clickBrowse(sandbox);
    await flush(10);
    ok(outputRoot(sandbox) === "C:\\Surveys", `expected the field untouched, got: ${outputRoot(sandbox)}`);
    ok(/type the folder path/i.test(outputRootNote(sandbox)), `got: ${outputRootNote(sandbox)}`);
  });

  await test("a dialog left open leaves the field alone and says nothing changed", async () => {
    const { sandbox } = await bootedWithPicker(() =>
      jsonResponse(504, { error: "The folder picker was open for 120 seconds with nothing chosen, so it was closed. Nothing has changed." })
    );
    clickBrowse(sandbox);
    await flush(10);
    ok(outputRoot(sandbox) === "C:\\Surveys", `expected the field untouched, got: ${outputRoot(sandbox)}`);
    ok(/nothing has changed/i.test(outputRootNote(sandbox)), `got: ${outputRootNote(sandbox)}`);
  });

  await test("typing a path still works when the picker is unavailable", async () => {
    // The whole point of the button being help rather than a control: a
    // machine with no picker must be exactly as usable as before.
    const { sandbox, fetchCalls } = await bootedWithPicker(() =>
      jsonResponse(503, { error: "not available. Type the folder path instead." })
    );
    clickBrowse(sandbox);
    await flush(10);
    fetchCalls.length = 0;

    setField(sandbox, "output-root", "E:\\Typed");
    await flush(10);

    const put = fetchCalls.find(
      (c) => c.url.pathname === "/api/config" && (c.options.method || "").toUpperCase() === "PUT"
    );
    ok(put, "expected a typed path to still be saved");
    ok(JSON.parse(put.options.body).output_root === "E:\\Typed");
  });

  await test("the browse button cannot be clicked twice while a dialog is open", async () => {
    // A second native dialog stacked on the first is a window nobody can
    // attribute an answer to. The server refuses one too (409); this is
    // the half that stops the request being made at all.
    let release;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    const { sandbox, fetchCalls } = await bootedWithPicker(async () => {
      await pending;
      return jsonResponse(200, { path: "D:\\NewSurveys" });
    });
    fetchCalls.length = 0;

    clickBrowse(sandbox);
    await flush(5);
    ok(
      sandbox.document.getElementById("output-root-browse").disabled === true,
      "expected the button disabled while the dialog is open"
    );
    ok(/look behind this window/i.test(outputRootNote(sandbox)), `got: ${outputRootNote(sandbox)}`);

    release();
    await flush(10);
    ok(fetchCalls.filter((c) => c.url.pathname === "/api/folder-dialog").length === 1);
    ok(outputRoot(sandbox) === "D:\\NewSurveys");
  });

  await test("the browse button comes back after a failure", async () => {
    const { sandbox } = await bootedWithPicker(() =>
      jsonResponse(503, { error: "not available. Type the folder path instead." })
    );
    clickBrowse(sandbox);
    await flush(10);
    ok(
      sandbox.document.getElementById("output-root-browse").disabled === false,
      "a picker that failed once must not leave the button dead for the session"
    );
  });

  // =======================================================================
  // Task 31, part 1: the destination belongs beside Download, not behind
  // the Settings button.
  //
  // These are assertions about the committed markup, not about the
  // sandbox, and they are the only kind that can be: this harness models
  // elements by id with no tree between them, so "above Download" and
  // "not inside the settings panel" are facts about index.html itself and
  // are checked there. Everything else about the field, that it persists,
  // that the picker writes to it, that a typed path reaches an estimate,
  // is already checked through the real app.js above and is unchanged by
  // the move, which is the point of the ids being stable.
  // =======================================================================

  // The settings panel's own markup, from its opening tag to the end of
  // the file's <div id="settings-panel"> block. Sliced at what follows it
  // rather than by counting nested tags, which a regex cannot do: the
  // assertion below only needs to know whether the picker's markup is
  // anywhere inside that region.
  //
  // That end marker used to be "<footer>", and Task 38, item 3 took the
  // footer away: the log moved into the map's own column so the form pane
  // could run the full height. indexOf would have returned -1 and slice
  // would have quietly read to one character from the end of the file,
  // which is a check that still passes while no longer being the check
  // it says it is. The scripts at the foot of the page are the marker
  // now, and they are the last thing in the body.
  const SETTINGS_PANEL_MARKUP = INDEX_HTML_MARKUP.slice(
    INDEX_HTML_MARKUP.indexOf('<div id="settings-panel"'),
    INDEX_HTML_MARKUP.indexOf("<script")
  );

  await test("the output root is out of the settings panel entirely, not copied out of it", () => {
    ok(
      SETTINGS_PANEL_MARKUP.length > 0 && SETTINGS_PANEL_MARKUP.includes('id="theme"'),
      "expected to have actually found the settings panel's markup to check"
    );
    ok(
      !SETTINGS_PANEL_MARKUP.includes('id="output-root"'),
      "the owner asked for this out of Settings; it is still in there"
    );
    ok(
      !SETTINGS_PANEL_MARKUP.includes('id="output-root-browse"'),
      "the Browse button is still in the settings panel"
    );
    // Moved, not mirrored. Two elements editing one value is two places
    // for a path to be typed and one question nobody can answer about
    // which of them a download uses; there is exactly one of each here,
    // so the question cannot be asked.
    const fields = INDEX_HTML_MARKUP.match(/id="output-root"/g) || [];
    ok(fields.length === 1, `expected exactly one output root field, found ${fields.length}`);
    const buttons = INDEX_HTML_MARKUP.match(/id="output-root-browse"/g) || [];
    ok(buttons.length === 1, `expected exactly one Browse button, found ${buttons.length}`);
  });

  await test("the text box, then the button, then Download, in the owner's own order", () => {
    const field = INDEX_HTML_MARKUP.indexOf('id="output-root"');
    const browse = INDEX_HTML_MARKUP.indexOf('id="output-root-browse"');
    const download = INDEX_HTML_MARKUP.indexOf('id="download"');
    ok(field !== -1 && browse !== -1 && download !== -1, "expected all three controls in the markup");
    ok(field < browse, "the text box must come above the button");
    ok(browse < download, "the button must sit just above Download");
    // And between the two: nothing else that draws a path. The folder
    // preview stays where it was, above the field, because it is the one
    // element that shows the whole destination including the dated,
    // named subfolder; a second copy of a path between the field and
    // Download would be the third path this page does not need.
    const between = INDEX_HTML_MARKUP.slice(browse, download);
    ok(
      !between.includes("folder-preview"),
      "the folder preview must not have been duplicated below the field"
    );
  });

  await test("the destination is usable without ever opening Settings", async () => {
    // The whole complaint. A picker reached only by opening a panel is
    // the thing that was wrong, so this drives the real Browse flow and
    // then asserts the panel was never involved.
    const { sandbox } = await bootedWithPicker(() => jsonResponse(200, { path: "D:\\NewSurveys" }));
    ok(
      sandbox.document.getElementById("settings-panel").hidden === true,
      "expected the settings panel closed at boot"
    );
    clickBrowse(sandbox);
    await flush(10);
    ok(outputRoot(sandbox) === "D:\\NewSurveys", `got: ${outputRoot(sandbox)}`);
    ok(
      sandbox.document.getElementById("settings-panel").hidden === true,
      "picking a folder must not have needed the settings panel opened"
    );
  });

  // =======================================================================
  // Task 31, part 2: a red tile says why it is red.
  //
  // Driven through the real poll loop wherever the answer depends on the
  // wiring rather than on the classification, because the classification
  // is the half that was easy: a summary that computes a perfect list of
  // reasons and never reaches a rectangle or a panel is exactly the
  // test-green-nothing-works shape this file exists to catch.
  // =======================================================================

  // Real reasons, in the shape Task 30 emits: kind for code, reason for
  // the owner, retried for whether it has already been asked twice. Two
  // different kinds, because "the explanation must be the recorded cause"
  // is only demonstrated by two failures that say different things.
  const OSM_TIMEOUT = {
    event: "tile_failed",
    source: "osm",
    tile_id: "r00_c01",
    kind: "timeout",
    reason: "the OpenStreetMap map API did not answer within 60 seconds, on all 4 attempts.",
    retried: 1,
  };
  const OSM_RATE_LIMITED = {
    event: "tile_failed",
    source: "osm",
    tile_id: "r00_c02",
    kind: "rate_limited",
    reason: "the OpenStreetMap map API answered HTTP 429, on all 4 attempts.",
    retried: 0,
  };

  function verifyDone(phase, failureEvents) {
    return {
      event: "verify_done",
      phase,
      checked: 4,
      ok: 4 - failureEvents.length,
      failed: failureEvents.length,
      pending: 0,
      corrections: [],
      failures: failureEvents.map(({ event, ...record }) => record),
    };
  }

  // The grid's own rectangles, which are the LAST ones created: setBBox
  // makes the committed extent's rectangle first, and renderTileGrid
  // clears and remakes one per tile on every estimate. The same slice the
  // Task 22 checks above already use, with liveness asserted rather than
  // assumed, so a change in drawing order fails here loudly instead of
  // quietly handing back dead rectangles that carry no tooltips and would
  // make every check below pass for nothing.
  function gridRectangles(sandbox, tileIds) {
    const found = sandbox.L._rectangles.slice(-tileIds.length);
    ok(
      found.length === tileIds.length && found.every((rect) => !rect.removed),
      "expected one live rectangle per tile at the end of the creation order"
    );
    return found;
  }

  function failureList(sandbox) {
    return sandbox.document.getElementById("tile-failures-list").innerHTML;
  }

  function failureHeading(sandbox) {
    return sandbox.document.getElementById("tile-failures-heading").textContent;
  }

  function failuresHidden(sandbox) {
    return sandbox.document.getElementById("tile-failures").hidden;
  }

  async function runWithFailures(events, { tileIds = tileIdsUpTo(4), polls } = {}) {
    const { sandbox, fetchCalls } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: polls || [{ state: "done", events }],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    return { sandbox, fetchCalls, tileIds };
  }

  await test("a failed tile explains itself with the cause that was recorded", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      { event: "tile_done", source: "osm", tile_id: tileIds[0] },
      OSM_TIMEOUT,
      { event: "tile_done", source: "osm", tile_id: tileIds[3] },
      { event: "source_done", source: "osm" },
    ]);

    ok(failuresHidden(sandbox) === false, "expected the failure list shown once a tile failed");
    ok(/1 tile did not download/.test(failureHeading(sandbox)), failureHeading(sandbox));
    const list = failureList(sandbox);
    ok(
      list.includes("did not answer within 60 seconds"),
      `expected the recorded cause in the list, got: ${list}`
    );
    ok(list.includes("osm r00_c01"), `expected the source and tile named, got: ${list}`);
    // Retried once and still short, which is the difference between a
    // service that was busy and one that is still saying no.
    ok(list.includes("Retried, and it failed again."), list);

    // And on the map, on the tile itself, which is where the owner is
    // already looking when they notice the colour.
    const rects = gridRectangles(sandbox, tileIds);
    ok(
      rects[1].tooltip && rects[1].tooltip.includes("did not answer within 60 seconds"),
      `expected the failed tile to carry its reason, got: ${rects[1].tooltip}`
    );
    ok(
      rects[1].tooltip.includes("Retried, and it failed again."),
      `the tooltip carries the whole sentence, not the reason without it: ${rects[1].tooltip}`
    );
    ok(rects[1].tooltipOptions.sticky === true, "expected a sticky tooltip over a large shape");
    for (const index of [0, 2, 3]) {
      ok(
        rects[index].tooltip === null,
        `tile ${index} did not fail and must carry no explanation, got: ${rects[index].tooltip}`
      );
    }
  });

  await test("two tiles that failed differently are not given the same explanation", async () => {
    // The brief's own line: if a tile was rate limited, say that, not the
    // sentence belonging to the tile that timed out.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      OSM_TIMEOUT,
      OSM_RATE_LIMITED,
      { event: "source_done", source: "osm" },
    ]);
    const list = failureList(sandbox);
    ok(/2 tiles did not download/.test(failureHeading(sandbox)), failureHeading(sandbox));
    ok(list.includes("did not answer within 60 seconds"), list);
    ok(list.includes("HTTP 429"), list);

    const rects = gridRectangles(sandbox, tileIds);
    ok(rects[1].tooltip.includes("did not answer within 60 seconds"), rects[1].tooltip);
    ok(rects[2].tooltip.includes("HTTP 429"), rects[2].tooltip);
    ok(
      !rects[2].tooltip.includes("60 seconds"),
      `the rate limited tile must not be handed the timeout's sentence: ${rects[2].tooltip}`
    );
    // The one that was not retried does not claim to have been.
    ok(
      !/Retried/.test(rects[2].tooltip),
      `retried: 0 must not read as retried, got: ${rects[2].tooltip}`
    );
  });

  await test("a tile the verify pass put right stops being red and loses its explanation", async () => {
    // The case only verify_done reports. A tile recorded failed whose
    // file the verify pass then finds on disk is corrected there and
    // nowhere else: there is no tile_done for it, so a browser reading
    // only tile_failed would show it red for good against a survey.json
    // saying the package is complete.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures(null, {
      tileIds,
      polls: [
        { state: "running", events: [OSM_TIMEOUT] },
        {
          state: "done",
          events: [
            OSM_TIMEOUT,
            verifyDone("after_retry", []),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    const rects = gridRectangles(sandbox, tileIds);
    ok(
      rects[1].tooltip && rects[1].tooltip.includes("60 seconds"),
      "expected the tile explained while it was still failed"
    );
    ok(failuresHidden(sandbox) === false, "expected the list shown on the first poll");

    await flush(800);
    ok(
      rects[1].tooltip === null,
      `expected the explanation taken off a tile no longer failed, got: ${rects[1].tooltip}`
    );
    ok(failuresHidden(sandbox) === true, "expected the list gone with the failure");
    ok(failureList(sandbox) === "", `expected the list emptied, got: ${failureList(sandbox)}`);
    ok(
      rects[1].options.fillColor !== "#8c3b2e",
      "expected the tile off the failed colour once the failure was retracted"
    );
  });

  await test("verify_done's verdict replaces what it no longer lists, one tile at a time", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      OSM_TIMEOUT,
      OSM_RATE_LIMITED,
      verifyDone("after_fetch", [OSM_TIMEOUT, OSM_RATE_LIMITED]),
      // The retry got the rate limited one and not the other.
      { event: "tile_done", source: "osm", tile_id: "r00_c02" },
      verifyDone("after_retry", [OSM_TIMEOUT]),
      { event: "source_done", source: "osm" },
    ]);
    ok(/1 tile did not download/.test(failureHeading(sandbox)), failureHeading(sandbox));
    const list = failureList(sandbox);
    ok(list.includes("r00_c01"), list);
    ok(!list.includes("r00_c02"), `the recovered tile must be gone from the list: ${list}`);
    const rects = gridRectangles(sandbox, tileIds);
    ok(rects[1].tooltip !== null, "the tile that is still short keeps its reason");
    ok(rects[2].tooltip === null, "the tile that landed loses its reason");
  });

  await test("a tile that came back empty is a success and is offered no explanation", async () => {
    // The owner was explicit: a rural tile keeps whatever it captured and
    // an empty one is a success. package.py never emits tile_failed for
    // it, and nothing here may invent one, so this is exactly what the
    // browser sees on that run: four ordinary tile_done events.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      verifyDone("after_fetch", []),
      { event: "source_done", source: "osm" },
    ]);
    ok(failuresHidden(sandbox) === true, "an empty tile must not produce a failure list");
    const rects = gridRectangles(sandbox, tileIds);
    for (const rect of rects) {
      ok(rect.tooltip === null, `expected no explanation on a successful tile, got: ${rect.tooltip}`);
      ok(
        rect.options.fillColor === "#2f5d4f",
        `expected the done colour, got ${rect.options.fillColor}`
      );
    }
  });

  await test("clicking a failed tile marks its entry, and clicking a good one clears the mark", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      OSM_TIMEOUT,
      OSM_RATE_LIMITED,
      { event: "source_done", source: "osm" },
    ]);
    const rects = gridRectangles(sandbox, tileIds);
    ok(!/selected/.test(failureList(sandbox)), "expected nothing marked before a click");

    rects[2].fire("click");
    const marked = failureList(sandbox);
    ok(/tile-failure selected/.test(marked), `expected the clicked tile's entry marked: ${marked}`);
    // The marked one is r00_c02's entry, not simply the first.
    const selectedEntry = marked.slice(marked.indexOf("tile-failure selected"));
    ok(
      selectedEntry.includes("r00_c02"),
      `expected the entry for the tile that was clicked: ${selectedEntry}`
    );

    rects[0].fire("click");
    ok(
      !/selected/.test(failureList(sandbox)),
      "clicking a tile that is fine must not leave a mark on another tile's entry"
    );
  });

  await test("a press that is drawing an extent is not also a question about a failure", async () => {
    // Leaflet passes a click on a rectangle up to the map as well, which
    // is what lets a new extent be drawn over an existing grid. Both
    // handlers see the same gesture, so the rectangle's own has to know
    // to stay out of the way.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([OSM_TIMEOUT, { event: "source_done", source: "osm" }]);
    const rects = gridRectangles(sandbox, tileIds);

    sandbox.document.getElementById("draw").fire("click");
    rects[1].fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    ok(
      !/selected/.test(failureList(sandbox)),
      "a click while the tool is armed must not have selected a failure as well"
    );
    ok(
      sandbox.document.getElementById("draw").textContent === "Release to finish",
      "expected the draw tool to have taken the press"
    );
  });

  await test("the click a finished drag leaves behind is not a question about a failure", async () => {
    // Task 36, item 2. The tool disarms on the release, so the click the
    // browser then fires on the common ancestor of the press and the
    // release arrives with the tool already disarmed. Leaflet suppresses
    // a click after a drag it handled itself, and map dragging is
    // switched off for exactly as long as this tool is armed, so there is
    // no Leaflet drag to suppress it here.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([OSM_TIMEOUT, { event: "source_done", source: "osm" }]);
    const rects = gridRectangles(sandbox, tileIds);

    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.42, lng: -3.28 } });
    rects[1].fire("click"); // the trailing click, on a rectangle the drag crossed
    ok(
      !/selected/.test(failureList(sandbox)),
      "the click left over by a finished drag must not select a failure"
    );

    // And the suppression lasts exactly one click: an ordinary click on a
    // red tile afterwards still answers "why is this one red".
    sandbox.L._mapObject.fire("click", { latlng: { lat: 51.42, lng: -3.28 } });
    rects[1].fire("click");
    ok(
      /selected/.test(failureList(sandbox)),
      "expected a plain click on a red tile to still mark its entry"
    );
  });

  await test("a reason from the server is escaped in both places it is shown", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      {
        event: "tile_failed",
        source: "osm",
        tile_id: "r00_c01",
        kind: "service_error",
        reason: "<img src=x onerror=alert(1)> answered HTTP 503.",
        retried: 0,
      },
      { event: "source_done", source: "osm" },
    ]);
    const list = failureList(sandbox);
    ok(!list.includes("<img"), `expected the tag escaped in the list, got: ${list}`);
    ok(list.includes("&lt;img"), `expected an escaped tag, got: ${list}`);
    // Leaflet assigns a string tooltip through innerHTML, so this one is
    // not merely tidiness.
    const tooltip = gridRectangles(sandbox, tileIds)[1].tooltip;
    ok(!tooltip.includes("<img"), `expected the tag escaped in the tooltip, got: ${tooltip}`);
    ok(tooltip.includes("&lt;img"), `expected an escaped tag, got: ${tooltip}`);
  });

  await test("a failure with no recorded reason names the gap instead of restating the colour", async () => {
    // Task 30 guarantees a reason on every tile_failed, so this is the
    // page meeting an older server. "This tile failed" is the one answer
    // it must not give, because the colour already said that.
    const { sandbox } = await runWithFailures([
      { event: "tile_failed", source: "osm", tile_id: "r00_c01" },
      { event: "source_done", source: "osm" },
    ]);
    const list = failureList(sandbox);
    ok(list.includes("No reason was recorded"), `got: ${list}`);
    ok(!list.includes("undefined"), `expected no undefined rendered at the owner: ${list}`);
  });

  await test("a second download does not inherit the last one's failures", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([OSM_TIMEOUT, { event: "source_done", source: "osm" }]);
    ok(failuresHidden(sandbox) === false, "expected the first run's failure shown");
    const rects = gridRectangles(sandbox, tileIds);
    ok(rects[1].tooltip !== null, "expected the first run's explanation bound");

    sandbox.document.getElementById("download").fire("click");
    await flush(10);
    ok(failuresHidden(sandbox) === true, "expected the list cleared the moment a new run starts");
    ok(
      rects[1].tooltip === null,
      `expected the previous run's explanation taken off the tile, got: ${rects[1].tooltip}`
    );
  });

  await test("a mark means the owner clicked it, never that this tile failed before", async () => {
    // The selection is pruned the moment the tile it points at stops
    // failing, and this is the case that makes that visible rather than
    // merely tidy: a tile that fails, is clicked, recovers, and then
    // fails again in a later run must come back UNmarked. A selection
    // that outlived its own failure would reappear as a mark the owner
    // never made, pointing at a tile they never asked about.
    const tileIds = tileIdsUpTo(4);
    const failedRun = { state: "done", events: [OSM_TIMEOUT, { event: "source_done", source: "osm" }] };
    // One poll settles each run, since every reply below is already
    // "done", so these three are the three downloads in order: it failed,
    // it was clean, it failed again.
    const { sandbox } = await runWithFailures(null, {
      tileIds,
      polls: [
        failedRun,
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            verifyDone("after_fetch", []),
            { event: "source_done", source: "osm" },
          ],
        },
        failedRun,
      ],
    });
    gridRectangles(sandbox, tileIds)[1].fire("click");
    ok(/tile-failure selected/.test(failureList(sandbox)), "expected the clicked entry marked");

    // The same tile recovers, which retracts the failure and with it the
    // only thing the mark was ever attached to.
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(failuresHidden(sandbox) === true, "expected no failures on the clean run");

    // And now it fails again, all on its own.
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(failuresHidden(sandbox) === false, "expected the fresh failure listed");
    ok(
      !/selected/.test(failureList(sandbox)),
      `a mark must not survive the failure it pointed at: ${failureList(sandbox)}`
    );
  });

  await test("verify_done's own lists are logged as readable JSON, not as [object Object]", async () => {
    // app.js formats no event by name, which is what makes a new event
    // from the Python side safe to add. verify_done is the first to carry
    // a LIST, and a list interpolated into a template literal is a field
    // the owner can do nothing with.
    const { sandbox } = await runWithFailures([
      verifyDone("after_retry", [OSM_TIMEOUT]),
      { event: "source_done", source: "osm" },
    ]);
    const lines = sandbox.document
      .getElementById("log")
      .children.map((line) => line.textContent);
    const verifyLine = lines.find((line) => line.startsWith("verify_done"));
    ok(verifyLine, `expected a verify_done log line, got: ${JSON.stringify(lines)}`);
    ok(!verifyLine.includes("[object Object]"), verifyLine);
    ok(verifyLine.includes('"tile_id":"r00_c01"'), verifyLine);
    ok(verifyLine.includes("phase=after_retry"), `a scalar must still print plainly: ${verifyLine}`);
  });

  // =======================================================================
  // Task 36, item 4: a tile that had to be split says so on the grid.
  // The parent is marked, not the quarters, because the client has never
  // computed tile geometry and tile_subdivided carries a count rather
  // than four rectangles (see app.js's TILE_SPLIT_STYLE).
  // =======================================================================

  const SPLIT_R00_C01 = {
    event: "tile_subdivided",
    source: "osm",
    tile_id: "r00_c01",
    pieces: 4,
    depth: 1,
  };

  function legendHtml(sandbox) {
    return sandbox.document.getElementById("tile-legend").innerHTML;
  }

  await test("a split tile is marked on the grid, and only that tile", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      SPLIT_R00_C01,
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      { event: "source_done", source: "osm" },
    ]);
    const rects = gridRectangles(sandbox, tileIds);
    ok(rects[1].options.dashArray === "5 4", `expected the split tile dashed, got ${rects[1].options.dashArray}`);
    ok(!rects[0].options.dashArray, `expected its neighbour undashed, got ${rects[0].options.dashArray}`);
    ok(!rects[2].options.dashArray, `expected its neighbour undashed, got ${rects[2].options.dashArray}`);
    // A split is not a failure and must not read as one: the tile still
    // finishes in the ordinary done colour, dashes and all.
    ok(
      rects[1].options.fillColor === "#2f5d4f",
      `expected the split tile to still finish done, got ${rects[1].options.fillColor}`
    );
  });

  await test("the legend explains the dashes, and only once something has split", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      { event: "source_done", source: "osm" },
    ]);
    ok(
      !/Split into pieces/.test(legendHtml(sandbox)),
      `a run with no split needs no entry for one: ${legendHtml(sandbox)}`
    );

    const split = await runWithFailures([
      SPLIT_R00_C01,
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      { event: "source_done", source: "osm" },
    ]);
    ok(
      /Split into pieces/.test(legendHtml(split.sandbox)),
      `expected the split entry once a tile has split: ${legendHtml(split.sandbox)}`
    );
  });

  await test("a split tile says how many pieces, in its own tooltip", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      SPLIT_R00_C01,
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      { event: "source_done", source: "osm" },
    ]);
    const rects = gridRectangles(sandbox, tileIds);
    ok(/4 pieces/.test(rects[1].tooltip), `expected the piece count, got ${rects[1].tooltip}`);
    ok(
      rects[1].tooltipOptions.className === "tile-note-tooltip",
      `a split is not a failure, so it gets the neutral box, got ${rects[1].tooltipOptions.className}`
    );
    ok(rects[0].tooltip === null, "a tile that did not split carries no tooltip");
  });

  await test("a tile that split AND failed says both things, in the failure box", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      SPLIT_R00_C01,
      OSM_TIMEOUT, // the same tile, r00_c01
      { event: "source_done", source: "osm" },
    ]);
    const rects = gridRectangles(sandbox, tileIds);
    ok(/4 pieces/.test(rects[1].tooltip), `expected the split sentence, got ${rects[1].tooltip}`);
    ok(/did not answer/.test(rects[1].tooltip), `expected the failure sentence too, got ${rects[1].tooltip}`);
    ok(
      rects[1].tooltipOptions.className === "tile-failure-tooltip",
      `a tile with a failure keeps the warning box, got ${rects[1].tooltipOptions.className}`
    );
  });

  await test("a quarter's own split is not a second split of the plan's tile", async () => {
    // OsmSource emits one tile_subdivided per SPLIT, and a quarter that
    // is itself too dense emits another under "<parent>_q10", which is
    // not a tile of the plan at all. The same guard that keeps the COUNT
    // honest (review finding I5) has to keep the grid honest too.
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00", "r00_c01"],
      ["osm"],
      [
        SPLIT_R00_C01,
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c01_q10", pieces: 4, depth: 2 },
      ],
      true
    );
    ok(summary.subdivisions === 1, `one tile was split, got ${summary.subdivisions}`);
    ok(summary.tileSubdivisions.size === 1, "expected exactly one marked tile");
    ok(
      summary.tileSubdivisions.get("r00_c01").pieces === 4,
      JSON.stringify(summary.tileSubdivisions.get("r00_c01"))
    );
    ok(
      !summary.tileSubdivisions.has("r00_c01_q10"),
      "a quarter must not become a rectangle of its own"
    );
  });

  await test("a split with no piece count says so rather than inventing a number", async () => {
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00"],
      ["osm"],
      [{ event: "tile_subdivided", source: "osm", tile_id: "r00_c00" }],
      true
    );
    ok(summary.tileSubdivisions.get("r00_c00").pieces === 0, "expected no fabricated count");
  });

  await test("a second download starts the grid over with no dashes and no legend entry", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      SPLIT_R00_C01,
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      { event: "source_done", source: "osm" },
    ]);
    const rects = gridRectangles(sandbox, tileIds);
    ok(rects[1].options.dashArray === "5 4", "expected the first run to have marked it");

    sandbox.document.getElementById("download").fire("click");
    await flush(10);
    ok(
      !rects[1].options.dashArray,
      `a fresh run over the same ground has not split anything yet, got ${rects[1].options.dashArray}`
    );
    ok(
      !/Split into pieces/.test(legendHtml(sandbox)),
      `expected the legend entry gone with it: ${legendHtml(sandbox)}`
    );
  });

  await test("changing the theme keeps a split tile's dashes", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures([
      SPLIT_R00_C01,
      ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
      { event: "source_done", source: "osm" },
    ]);
    const rects = gridRectangles(sandbox, tileIds);
    setField(sandbox, "theme", "dark");
    await flush(10);
    ok(
      rects[1].options.fillColor === "#6fc3a3",
      `expected the dark done colour, got ${rects[1].options.fillColor}`
    );
    ok(
      rects[1].options.dashArray === "5 4",
      `expected the dashes to survive a repaint, got ${rects[1].options.dashArray}`
    );
  });

  // =======================================================================
  // Task 38, item 1: the Site field is labelled "Site name".
  //
  // A label rename is one word of markup and one whole class of silent
  // damage if it reaches any further: the id, the request key and the
  // config the owner already has on disk all have to be exactly what they
  // were, or a rename nobody asked for empties a field that was full.
  // =======================================================================

  await test("the Site field is labelled Site name", () => {
    const match = INDEX_HTML_MARKUP.match(/<label>\s*([^<]*?)\s*<input id="site"/);
    ok(match, "expected a label wrapping the site input in the committed markup");
    ok(match[1] === "Site name", `expected "Site name", got ${JSON.stringify(match[1])}`);
  });

  await test("renaming the label renamed nothing the server or a saved config reads", async () => {
    // The half of the rename that could break the owner's machine. The id
    // is what every $() call and every querySelector in app.js resolves
    // through, and `site` is the key SurveyRequest reads and
    // mapgen.naming slugifies into the folder name; a page that started
    // sending `site_name` would fail every estimate against the server
    // already installed.
    ok(INPUT_MARKUP.has("site"), "expected the input to keep id=site");
    const { sandbox, fetchCalls } = await bootedSandbox((url) => {
      if (url.pathname === "/api/estimate") {
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 175,
          warnings: [],
          folder: "C:\\out",
          tile_grid: [],
          sources: [],
        });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry Waterfront");
    await flush(20);
    const call = fetchCalls.filter((c) => c.url.pathname === "/api/estimate").pop();
    ok(call, "expected an estimate request once both names are filled");
    const sent = JSON.parse(call.options.body);
    ok(sent.site === "Barry Waterfront", `expected the site under the "site" key, got ${JSON.stringify(sent)}`);
    ok(sent.region === "South Wales", `expected the region unchanged, got ${JSON.stringify(sent)}`);
  });

  // =======================================================================
  // Task 38, item 2: a drawn rectangle can be edited. Four corner handles
  // resize it, a drag of the body moves it, and both go through the same
  // one commit the draw tool uses.
  //
  // The gestures are driven the way the real build produces them, which
  // for a corner means Leaflet's own marker drag (press, dragstart on the
  // first move, the marker's latlng set before each drag event, dragend)
  // and for the body means the map's own mousedown/mousemove/mouseup. The
  // press on a handle deliberately fires on the map as well unless app.js
  // stops it, because that is what Leaflet does and it is the one thing
  // that would make every resize a move too.
  // =======================================================================

  const EDIT_BBOX = "-3.3,51.4,-3.2,51.5";

  function extentGrid() {
    return [{ tile_id: "r00_c00", west: -3.3, south: 51.4, east: -3.2, north: 51.5 }];
  }

  async function editableSandbox() {
    const extentCalls = [];
    const { sandbox, fetchCalls } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/extent") {
        extentCalls.push(JSON.parse(options.body));
        return jsonResponse(200, {
          tiles: 1,
          rows: 1,
          cols: 1,
          extent_km: { width: 7, height: 11 },
          tile_grid: extentGrid(),
        });
      }
      return null;
    });
    setField(sandbox, "bbox", EDIT_BBOX);
    await flush(10);
    extentCalls.length = 0;
    return { sandbox, fetchCalls, extentCalls };
  }

  function liveHandles(sandbox) {
    return sandbox.L._markers.filter((marker) => !marker.removed);
  }

  function handleFor(sandbox, corner) {
    return liveHandles(sandbox).find((marker) =>
      String(((marker.options.icon || {}).options || {}).className || "").includes(
        `extent-handle-${corner}`
      )
    );
  }

  function bboxNumbers(sandbox) {
    return sandbox.document
      .getElementById("bbox")
      .value.split(",")
      .map((part) => parseFloat(part));
  }

  function closeTo(actual, expected, what) {
    ok(
      Math.abs(actual - expected) < 1e-6,
      `${what}: expected ${expected}, got ${actual}`
    );
  }

  await test("a committed extent gets four corner handles, one on each corner", async () => {
    const { sandbox } = await editableSandbox();
    const handles = liveHandles(sandbox);
    ok(handles.length === 4, `expected four handles, got ${handles.length}`);
    const corners = handles
      .map((marker) => `${marker.getLatLng().lat},${marker.getLatLng().lng}`)
      .sort();
    ok(
      corners.join(" | ") === ["51.5,-3.3", "51.5,-3.2", "51.4,-3.2", "51.4,-3.3"].sort().join(" | "),
      `expected the four corners of the box, got ${corners.join(" | ")}`
    );
    // Four corners and no edge midpoints, which is what was asked for.
    ok(handleFor(sandbox, "nw") && handleFor(sandbox, "se"), "expected the corners to be named");
  });

  await test("the handles move with a new extent rather than a fifth being added", async () => {
    // Every way of setting an extent goes through setBBox, so a capture
    // of the viewport has to leave four handles on the new box, not four
    // on the old one and four more on this.
    const { sandbox } = await editableSandbox();
    sandbox.L._mapObject._setBounds({ west: -3.25, south: 51.45, east: -3.15, north: 51.52 });
    sandbox.document.getElementById("viewport").fire("click");
    await flush(10);
    const handles = liveHandles(sandbox);
    ok(handles.length === 4, `expected still four handles, got ${handles.length}`);
    ok(
      handles.every((marker) => [51.45, 51.52].includes(marker.getLatLng().lat)),
      `expected every handle on the new box, got ${handles.map((m) => m.getLatLng().lat).join()}`
    );
  });

  await test("dragging a corner resizes the extent and leaves the opposite corner where it was", async () => {
    const { sandbox, extentCalls } = await editableSandbox();
    handleFor(sandbox, "nw")._dragTo({ lat: 51.55, lng: -3.35 });
    await flush(10);
    const [west, south, east, north] = bboxNumbers(sandbox);
    closeTo(west, -3.35, "west followed the corner");
    closeTo(north, 51.55, "north followed the corner");
    closeTo(east, -3.2, "east is the anchor and must not move");
    closeTo(south, 51.4, "south is the anchor and must not move");
    // And the grid follows the new extent, exactly as it does for a
    // freshly drawn one: the same estimate that redraws it.
    ok(extentCalls.length === 1, `expected one estimate for the finished edit, got ${extentCalls.length}`);
    ok(
      extentCalls[0].bbox === "-3.35,51.4,-3.2,51.55",
      `expected the edited extent estimated, got ${extentCalls[0].bbox}`
    );
  });

  await test("nothing is estimated per pixel: the request waits for the release", async () => {
    const { sandbox, extentCalls } = await editableSandbox();
    const handle = handleFor(sandbox, "se");
    // The gesture, opened by hand so the middle of it can be inspected.
    handle._press();
    handle.fire("dragstart", { latlng: handle.getLatLng() });
    for (const lat of [51.41, 51.42, 51.43, 51.44]) {
      handle.setLatLng({ lat, lng: -3.21 });
      handle.fire("drag", { latlng: handle.getLatLng() });
    }
    await flush(10);
    ok(extentCalls.length === 0, `expected no estimate mid-drag, got ${extentCalls.length}`);
    // The rectangle and the field still follow every step, so the drag is
    // not silently doing nothing.
    const [, south] = bboxNumbers(sandbox);
    closeTo(south, 51.44, "the field follows the cursor mid-drag");
    handle.fire("dragend", { latlng: handle.getLatLng() });
    await flush(10);
    ok(extentCalls.length === 1, `expected exactly one estimate on the release, got ${extentCalls.length}`);
  });

  await test("a corner dragged past the opposite one takes the box with it, anchor still fixed", async () => {
    // The anchor has to be captured when the gesture starts. Recomputed
    // per move, "the corner opposite the north west" becomes the corner
    // under the cursor the moment the drag crosses it, and the box would
    // walk away.
    const { sandbox } = await editableSandbox();
    handleFor(sandbox, "nw")._dragTo({ lat: 51.45, lng: -3.25 }, { lat: 51.3, lng: -3.1 });
    await flush(10);
    const [west, south, east, north] = bboxNumbers(sandbox);
    closeTo(west, -3.2, "the anchor's longitude is now the west edge");
    closeTo(east, -3.1, "the cursor is now the east edge");
    closeTo(south, 51.3, "the cursor is now the south edge");
    closeTo(north, 51.4, "the anchor's latitude is now the north edge");
  });

  await test("a corner dragged onto its anchor's own line commits nothing", async () => {
    // A zero-area box, refused where the draw tool refuses its own and
    // where a pasted one is refused, leaving the committed extent alone.
    const { sandbox, extentCalls } = await editableSandbox();
    handleFor(sandbox, "nw")._dragTo({ lat: 51.4, lng: -3.25 }); // onto the anchor's latitude
    await flush(10);
    ok(
      sandbox.document.getElementById("bbox").value === EDIT_BBOX,
      `expected the extent untouched, got ${sandbox.document.getElementById("bbox").value}`
    );
    ok(extentCalls.length === 0, `expected no estimate for a rejected edit, got ${extentCalls.length}`);
  });

  await test("pressing a corner does not also take hold of the body underneath it", async () => {
    // Every corner of a rectangle is a point inside that rectangle, so
    // without app.js stopping the press at the marker this gesture is a
    // resize and a move at once. The stub fires the map's own mousedown
    // after the marker's unless it is stopped, which is what Leaflet
    // does, so a page that forgets leaves a body drag open here.
    const { sandbox } = await editableSandbox();
    handleFor(sandbox, "nw")._dragTo({ lat: 51.55, lng: -3.35 });
    await flush(10);
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "a body drag was left open by the press on the handle: panning is still switched off"
    );
    const [west, south, east, north] = bboxNumbers(sandbox);
    closeTo(west, -3.35, "west");
    closeTo(south, 51.4, "south");
    closeTo(east, -3.2, "east");
    closeTo(north, 51.55, "north");
  });

  await test("dragging the body moves the whole extent and keeps its size", async () => {
    const { sandbox, extentCalls } = await editableSandbox();
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.45, lng: -3.25 } });
    ok(
      sandbox.L._mapObject.dragging.enabled() === false,
      "expected map panning switched off for the length of the move"
    );
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.46, lng: -3.24 } });
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.46, lng: -3.24 } });
    await flush(10);
    const [west, south, east, north] = bboxNumbers(sandbox);
    closeTo(west, -3.29, "west moved by the drag");
    closeTo(east, -3.19, "east moved by the same amount");
    closeTo(south, 51.41, "south moved by the drag");
    closeTo(north, 51.51, "north moved by the same amount");
    closeTo(east - west, 0.1, "the width is unchanged");
    closeTo(north - south, 0.1, "the height is unchanged");
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "expected panning back on once the move is over"
    );
    ok(extentCalls.length === 1, `expected one estimate for the move, got ${extentCalls.length}`);
    // The handles came with it.
    const nw = handleFor(sandbox, "nw").getLatLng();
    closeTo(nw.lat, 51.51, "the north west handle followed");
    closeTo(nw.lng, -3.29, "the north west handle followed");
  });

  await test("a press outside the extent is not a move, and still pans the map", async () => {
    const { sandbox, extentCalls } = await editableSandbox();
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 52.5, lng: -2.0 } });
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "a press on open map must leave Leaflet's own panning alone"
    );
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 52.6, lng: -2.1 } });
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 52.6, lng: -2.1 } });
    await flush(10);
    ok(
      sandbox.document.getElementById("bbox").value === EDIT_BBOX,
      `expected the extent untouched, got ${sandbox.document.getElementById("bbox").value}`
    );
    ok(extentCalls.length === 0, "expected no estimate from a press that was never on the extent");
  });

  await test("a press inside the extent that never moves commits nothing and keeps its click", async () => {
    // That click is how a red tile is selected in the failure list, so a
    // press that turned out not to be a drag must not swallow it.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await runWithFailures(null, {
      tileIds,
      polls: [
        {
          state: "done",
          events: [OSM_TIMEOUT, { event: "source_done", source: "osm" }],
        },
      ],
    });
    const before = sandbox.document.getElementById("bbox").value;
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.385, lng: -3.285 } });
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.385, lng: -3.285 } });
    ok(
      sandbox.document.getElementById("bbox").value === before,
      "a press with no movement must commit nothing"
    );
    gridRectangles(sandbox, tileIds)[1].fire("click");
    sandbox.L._mapObject.fire("click", { latlng: { lat: 51.385, lng: -3.285 } });
    ok(
      /tile-failure selected/.test(failureList(sandbox)),
      `expected the click to still select the failed tile: ${failureList(sandbox)}`
    );
  });

  await test("Escape puts back an extent being moved and leaves panning on", async () => {
    const { sandbox, extentCalls } = await editableSandbox();
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.45, lng: -3.25 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.48, lng: -3.22 } });
    sandbox.document.fire("keydown", { key: "Escape" });
    ok(
      sandbox.document.getElementById("bbox").value === EDIT_BBOX,
      `expected the committed extent back, got ${sandbox.document.getElementById("bbox").value}`
    );
    ok(sandbox.L._mapObject.dragging.enabled() === true, "expected panning back on");
    // And the release that follows a cancelled move commits nothing.
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.48, lng: -3.22 } });
    await flush(10);
    ok(sandbox.document.getElementById("bbox").value === EDIT_BBOX, "a release after Escape commits nothing");
    ok(extentCalls.length === 0, `expected no estimate at all, got ${extentCalls.length}`);
  });

  await test("a release the map never saw ends the move on the next report of no button", async () => {
    // A mouseup off the window, or over a native folder dialog, never
    // reaches the map. Without this the rectangle would follow the cursor
    // with nothing held down.
    const { sandbox } = await editableSandbox();
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.45, lng: -3.25 } });
    sandbox.L._mapObject.fire("mousemove", {
      latlng: { lat: 51.46, lng: -3.24 },
      originalEvent: { buttons: 1 },
    });
    sandbox.L._mapObject.fire("mousemove", {
      latlng: { lat: 51.47, lng: -3.23 },
      originalEvent: { buttons: 0 },
    });
    await flush(10);
    ok(sandbox.L._mapObject.dragging.enabled() === true, "expected the stuck drag ended");
    const [, south] = bboxNumbers(sandbox);
    closeTo(south, 51.41, "the work done before the lost release is kept");
    // And the map is genuinely free again: a further move changes nothing.
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.9, lng: -3.9 } });
    const [, stillSouth] = bboxNumbers(sandbox);
    closeTo(stillSouth, 51.41, "the rectangle must not follow the cursor after the drag ended");
  });

  await test("the extent cannot be edited while a download is running", async () => {
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        { state: "running", events: [{ event: "tile_done", source: "osm", tile_id: tileIds[0] }] },
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    ok(liveHandles(sandbox).length === 4, "expected handles before the run");
    const before = sandbox.document.getElementById("bbox").value;
    sandbox.document.getElementById("download").fire("click");
    await flush(10);
    ok(
      liveHandles(sandbox).length === 0,
      `expected no handles on the map mid-run, got ${liveHandles(sandbox).length}`
    );
    // And the body cannot be taken hold of either, handles or no handles.
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.385, lng: -3.285 } });
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "a press mid-run must not take hold of the extent"
    );
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.4, lng: -3.27 } });
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.4, lng: -3.27 } });
    ok(
      sandbox.document.getElementById("bbox").value === before,
      `expected the extent untouched mid-run, got ${sandbox.document.getElementById("bbox").value}`
    );
    // The run ends and the extent is the owner's again.
    await flush(1500);
    ok(
      liveHandles(sandbox).length === 4,
      `expected the handles back once the run ended, got ${liveHandles(sandbox).length}`
    );
  });

  // --- the fix round: every way of changing the extent, not just three ---
  //
  // Item 2 locked the corner handles during a run and left Draw extent,
  // Select viewport, the bbox field and the place search able to replace
  // the extent, which is the worst way round: the gesture that does least
  // damage was the one that was stopped. They all end at setBBox, so the
  // refusal is there, and the controls are disabled so that nothing which
  // is refused still looks live.

  // A sandbox with a job that runs for one poll and then finishes, so a
  // test can look at the page during a run and after it.
  async function runningJobSandbox() {
    const tileIds = tileIdsUpTo(4);
    const { sandbox, fetchCalls } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        { state: "running", events: [{ event: "tile_done", source: "osm", tile_id: tileIds[0] }] },
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(10);
    return { sandbox, fetchCalls, tileIds };
  }

  await test("every control that changes the extent goes dead while a download runs", async () => {
    const { sandbox } = await runningJobSandbox();
    for (const id of ["draw", "viewport", "bbox", "place"]) {
      ok(
        sandbox.document.getElementById(id).disabled === true,
        `expected #${id} disabled for the length of the run`
      );
    }
    // And live again the moment it ends, by the same one call.
    await flush(1500);
    for (const id of ["draw", "viewport", "bbox", "place"]) {
      ok(
        sandbox.document.getElementById(id).disabled === false,
        `expected #${id} live again once the run ended`
      );
    }
  });

  await test("Draw extent cannot arm mid-run, and arms again afterwards", async () => {
    // The harness fires a listener whether or not the element is
    // disabled, which a browser does not, so this is the guard inside the
    // handler being tested rather than the attribute: it is what covers a
    // click already on its way when the job started.
    const { sandbox } = await runningJobSandbox();
    sandbox.document.getElementById("draw").fire("click");
    ok(
      sandbox.document.getElementById("draw").className === "",
      "expected the tool not to arm mid-run"
    );
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "a refused arm must not switch the map's own panning off"
    );
    await flush(1500);
    sandbox.document.getElementById("draw").fire("click");
    ok(
      sandbox.document.getElementById("draw").className === "armed",
      "expected the tool to arm again once the run ended"
    );
  });

  await test("Select viewport cannot capture mid-run, and captures again afterwards", async () => {
    const { sandbox } = await runningJobSandbox();
    const before = sandbox.document.getElementById("bbox").value;
    sandbox.L._mapObject._setBounds({ west: -1.5, south: 53.1, east: -1.4, north: 53.2 });
    // Counted, not just checked for an effect. setBBox refuses whatever
    // this hands it, so a version with no guard of its own would leave
    // the extent alone anyway and look identical from the outside; what
    // says the button is dead rather than merely harmless is that it does
    // not so much as read the map.
    const realGetBounds = sandbox.L._mapObject.getBounds;
    let reads = 0;
    sandbox.L._mapObject.getBounds = function countingGetBounds() {
      reads += 1;
      return realGetBounds.call(this);
    };
    sandbox.document.getElementById("viewport").fire("click");
    ok(reads === 0, "expected the button to do nothing at all mid-run, not to be refused later");
    sandbox.L._mapObject.getBounds = realGetBounds;
    ok(
      sandbox.document.getElementById("bbox").value === before,
      `expected the extent untouched mid-run, got ${sandbox.document.getElementById("bbox").value}`
    );
    await flush(1500);
    sandbox.document.getElementById("viewport").fire("click");
    ok(
      sandbox.document.getElementById("bbox").value === "-1.5,53.1,-1.4,53.2",
      `expected the capture to work once the run ended, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  await test("a pasted bbox cannot replace the extent mid-run", async () => {
    const { sandbox } = await runningJobSandbox();
    const rectanglesBefore = sandbox.L._rectangles.length;
    setField(sandbox, "bbox", "-1.5,53.1,-1.4,53.2");
    await flush(10);
    ok(
      sandbox.L._rectangles.length === rectanglesBefore,
      "expected no new extent rectangle drawn from a paste mid-run"
    );
  });

  await test("choosing a place cannot replace the extent mid-run", async () => {
    // choosePlaceMatch fills the region and site fields BEFORE it calls
    // setBBox, so this is the one of the four where a refusal at setBBox
    // alone would still have left two fields rewritten. The field being
    // disabled is what stops the choice being offered at all.
    const { sandbox } = await runningJobSandbox();
    ok(
      sandbox.document.getElementById("place").disabled === true,
      "expected the place search dead for the length of the run"
    );
    const rectanglesBefore = sandbox.L._rectangles.length;
    sandbox.choosePlaceMatch({
      display_name: "Sheffield",
      site: "Sheffield",
      region: "South Yorkshire",
      west: -1.5,
      south: 53.1,
      east: -1.4,
      north: 53.2,
    });
    await flush(10);
    ok(
      sandbox.L._rectangles.length === rectanglesBefore,
      "expected no new extent rectangle from a chosen place mid-run"
    );
  });

  await test("setBBox itself is what refuses, so a fifth way in is refused too", async () => {
    // The controls are the visible half. This is the half that makes the
    // claim true: the next thing on this page that learns to set an
    // extent is refused without having to remember to ask.
    const { sandbox } = await runningJobSandbox();
    const rectanglesBefore = sandbox.L._rectangles.length;
    const before = sandbox.document.getElementById("bbox").value;
    sandbox.setBBox({ west: -1.5, south: 53.1, east: -1.4, north: 53.2 }, false);
    await flush(10);
    ok(
      sandbox.document.getElementById("bbox").value === before,
      `expected setBBox to refuse outright mid-run, got ${sandbox.document.getElementById("bbox").value}`
    );
    ok(sandbox.L._rectangles.length === rectanglesBefore, "expected no rectangle drawn either");
  });

  await test("a download starting puts away a half-drawn rectangle", async () => {
    // Otherwise the tool is left armed behind a button that can no longer
    // disarm it, with the map unpannable for the length of the run.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [{ state: "running", events: [] }],
    });
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("mousedown", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.L._rectangles.some((r) => !r.removed), "expected a live preview before the download");
    sandbox.document.getElementById("download").fire("click");
    await flush(10);
    ok(sandbox.document.getElementById("draw").className === "", "expected the tool disarmed");
    ok(
      sandbox.L._mapObject.dragging.enabled() === true,
      "expected the map pannable again, not stranded by an armed tool"
    );
    // And the release that follows commits nothing.
    sandbox.L._mapObject.fire("mouseup", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(
      sandbox.document.getElementById("bbox").value === "-3.29,51.38,-3.28,51.39",
      `expected the run's own extent untouched, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  await test("a run that is stopped or fails hands the controls back too", async () => {
    // Every end, not just the tidy one: this is the same call on the same
    // line of the poll loop, and a stop is the end the owner makes most.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        { state: "running", events: [] },
        { state: "stopped", events: [{ event: "tile_done", source: "osm", tile_id: tileIds[0] }] },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(10);
    ok(sandbox.document.getElementById("draw").disabled === true, "expected them locked during the run");
    await flush(1500);
    for (const id of ["draw", "viewport", "bbox", "place"]) {
      ok(
        sandbox.document.getElementById(id).disabled === false,
        `expected #${id} live again after a stopped run`
      );
    }
    ok(liveHandles(sandbox).length === 4, "expected the corner handles back after a stopped run");
  });

  await test("losing contact with a job hands the controls back as well", async () => {
    // The page cannot know whether that job is still going, but it has
    // stopped watching it, so nothing here can be made inconsistent any
    // more. Leaving the extent locked would need a reload to undo.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [{ state: "running", events: [] }, { httpError: true }],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(1600);
    for (const id of ["draw", "viewport", "bbox", "place"]) {
      ok(
        sandbox.document.getElementById(id).disabled === false,
        `expected #${id} live again once nothing is watching the job`
      );
    }
  });

  // --- and the grid keyed to that geometry -------------------------------
  //
  // The second half of the same claim. The extent lock above protects the
  // geometry a job was started with; this protects the rectangles keyed
  // to it. refreshEstimate is bound to a change on seven other controls,
  // and any of them mid-run used to reach renderTileGrid with a different
  // tiling, which threw away the running job's rectangles and built new
  // ones keyed by tile ids that job's events do not name: every tile read
  // pending and the bar fell to 0% for the rest of a healthy run.

  // A job sandbox whose estimate answers differently the second time, so
  // a test can tell "the grid was left alone" from "the grid was redrawn
  // with the same thing".
  async function midRunEstimateSandbox({ polls }) {
    const tileIds = tileIdsUpTo(4);
    const later = {
      tiles: 9,
      rows: 3,
      cols: 3,
      extent_km: { width: 3, height: 3 },
      bytes_estimate: 9000,
      seconds_estimate: 400,
      warnings: [],
      folder: "C:\\out",
      tile_grid: gridFor(tileIdsUpTo(9)),
      sources: [{ id: "osm", seconds_estimate: 144 }],
    };
    let estimates = 0;
    let pollIndex = 0;
    const { sandbox } = await bootedSandbox(async (url, options) => {
      const method = (options.method || "GET").toUpperCase();
      if (url.pathname === "/api/sources") return jsonResponse(200, [TWO_SOURCES[0]]);
      if (url.pathname === "/api/config" && method === "PUT") return jsonResponse(200, DEFAULT_CONFIG);
      if (url.pathname === "/api/estimate") {
        estimates += 1;
        if (estimates > 1) return jsonResponse(200, later);
        return jsonResponse(200, {
          tiles: 4,
          rows: 1,
          cols: 4,
          extent_km: { width: 1, height: 1 },
          bytes_estimate: 1000,
          seconds_estimate: 175,
          warnings: [],
          folder: "C:\\out",
          tile_grid: gridFor(tileIds),
          sources: [{ id: "osm", seconds_estimate: 144 }],
        });
      }
      if (url.pathname === "/api/jobs" && method === "POST") return jsonResponse(202, { id: "job1" });
      if (url.pathname === "/api/jobs/job1") {
        const reply = polls[Math.min(pollIndex, polls.length - 1)];
        pollIndex += 1;
        return jsonResponse(200, { id: "job1", error: null, result_root: "C:\\out", ...reply });
      }
      return null;
    });
    setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
    await flush(10);
    setField(sandbox, "region", "South Wales");
    setField(sandbox, "site", "Barry");
    await flush(20);
    return { sandbox, tileIds };
  }

  const RUNNING_FIRST_TILE = {
    state: "running",
    events: [{ event: "tile_done", source: "osm", tile_id: "r00_c00" }],
  };

  await test("a re-estimate mid-run leaves the running job's grid exactly as it is", async () => {
    const { sandbox, tileIds } = await midRunEstimateSandbox({ polls: [RUNNING_FIRST_TILE] });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const painted = gridRectangles(sandbox, tileIds);
    ok(painted[0].options.fillColor === "#2f5d4f", "expected the run's first tile green before the estimate");
    const rectanglesBefore = sandbox.L._rectangles.length;

    // The ordinary thing to do while waiting for a download.
    setField(sandbox, "overlap", "250");
    await flush(20);

    ok(
      sandbox.L._rectangles.length === rectanglesBefore,
      `expected no rectangle rebuilt mid-run, ${sandbox.L._rectangles.length - rectanglesBefore} were`
    );
    const after = gridRectangles(sandbox, tileIds);
    ok(after.length === 4, `expected the job's own four tiles still on the map, got ${after.length}`);
    ok(
      after[0].options.fillColor === "#2f5d4f",
      `expected the finished tile still green, got ${after[0].options.fillColor}`
    );
    ok(after.every((rect) => !rect.removed), "expected none of the job's rectangles taken off the map");
  });

  await test("the estimate itself still runs mid-job, and still answers", async () => {
    // Asking what a different tiling would cost harms nothing, so the
    // panel updates. Only the rectangles describing the run in flight are
    // held back.
    const { sandbox } = await midRunEstimateSandbox({ polls: [RUNNING_FIRST_TILE] });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    setField(sandbox, "overlap", "250");
    await flush(20);
    const shown = sandbox.document.getElementById("estimate").innerHTML;
    ok(/9 tiles/.test(shown), `expected the panel to answer with the new tiling, got ${shown}`);
    ok(!/error/.test(sandbox.document.getElementById("estimate").className), "and not as an error");
  });

  await test("an estimate that fails mid-run does not clear the grid either", async () => {
    // The other way in, and the reason showEstimateError goes through the
    // same one sink: a failed estimate is exactly as much the running
    // job's business as a successful one.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [RUNNING_FIRST_TILE],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(gridRectangles(sandbox, tileIds).length === 4, "expected the job's grid up");
    sandbox.showEstimateError("Something the server refused.");
    ok(
      gridRectangles(sandbox, tileIds).length === 4,
      "expected the running job's grid to survive an estimate failure"
    );
    ok(
      /refused/.test(sandbox.document.getElementById("estimate").textContent),
      "expected the failure still reported in the panel"
    );
  });

  await test("the grid is free again the moment the run ends, like the extent", async () => {
    const { sandbox, tileIds } = await midRunEstimateSandbox({
      polls: [
        RUNNING_FIRST_TILE,
        {
          state: "done",
          events: [
            ...tileIdsUpTo(4).map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(1600);
    ok(gridRectangles(sandbox, tileIds).length === 4, "expected the finished run's grid still up");
    setField(sandbox, "overlap", "250");
    await flush(20);
    // Nine now, not four: the same estimate that was held back mid-run
    // redraws the moment the run is over.
    const redrawn = gridRectangles(sandbox, tileIdsUpTo(9));
    ok(redrawn.length === 9, `expected the new tiling drawn once the run ended, got ${redrawn.length}`);
  });

  await test("a stopped run frees the grid at the same instant it frees the extent", async () => {
    const { sandbox, tileIds } = await midRunEstimateSandbox({
      polls: [RUNNING_FIRST_TILE, { state: "stopped", events: [] }],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(1600);
    ok(
      sandbox.document.getElementById("draw").disabled === false,
      "expected the extent controls handed back by a stopped run"
    );
    // The stopped run's own rectangles, held onto before the estimate
    // that replaces them: "the grid was freed" means these come OFF the
    // map, which counting the new ones cannot say on its own.
    const stoppedRunGrid = gridRectangles(sandbox, tileIds);
    setField(sandbox, "overlap", "250");
    await flush(20);
    ok(
      gridRectangles(sandbox, tileIdsUpTo(9)).length === 9,
      "expected the grid handed back by the same stop"
    );
    ok(
      stoppedRunGrid.every((rect) => rect.removed),
      "expected the stopped run's own rectangles taken off the map by the new tiling"
    );
  });

  await test("Download cannot be pressed mid-run, so the log cannot be wiped", async () => {
    // A mid-run estimate used to re-enable it, and the handler's own
    // first line is $("log").innerHTML = "". One press would have thrown
    // away the log of the run in progress and then been refused by the
    // server with a 409.
    const { sandbox } = await midRunEstimateSandbox({ polls: [RUNNING_FIRST_TILE] });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const lines = sandbox.document.getElementById("log").children.length;
    ok(lines > 0, "expected the run's events in the log");
    setField(sandbox, "overlap", "250");
    await flush(20);
    ok(
      sandbox.document.getElementById("download").disabled === true,
      "a mid-run estimate must not hand the button back"
    );
    sandbox.document.getElementById("download").fire("click");
    ok(
      sandbox.document.getElementById("log").children.length >= lines,
      "the running job's log must survive a press on a button that should be dead"
    );
  });

  await test("a download that IS allowed still starts with an empty log", async () => {
    // The other half of the check above, and the reason it is not passing
    // for the wrong reason: a press that goes through clears the log, so
    // "the log survived" says the press was refused rather than saying
    // that pressing Download never clears anything.
    const { sandbox, tileIds } = await midRunEstimateSandbox({
      polls: [
        {
          state: "done",
          events: [
            ...tileIdsUpTo(4).map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const first = sandbox.document.getElementById("log").children.map((line) => line.textContent);
    ok(first.length > 0, "expected the first run's events in the log");
    ok(tileIds.length === 4, "fixture check");

    sandbox.document.getElementById("download").fire("click");
    ok(
      sandbox.document.getElementById("log").children.length === 0,
      `expected a fresh run to start from an empty log, got ${sandbox.document.getElementById("log").children.length} lines`
    );
  });

  await test("an armed draw tool takes the handles off the old rectangle", async () => {
    const { sandbox } = await editableSandbox();
    sandbox.document.getElementById("draw").fire("click");
    ok(liveHandles(sandbox).length === 0, "expected no handles while a new rectangle is being drawn");
    sandbox.document.fire("keydown", { key: "Escape" });
    ok(liveHandles(sandbox).length === 4, "expected them back once the tool is put away");
  });

  await test("the handles are drawn from the page's own palette, so both themes are covered", () => {
    // A DIV icon and not a Leaflet path, which is what lets the two
    // palettes at the top of the stylesheet cover these without a second
    // set of colours in JS the way the tile grid needs.
    const handle = cssRule(".extent-handle");
    ok(/var\(--surface\)/.test(handle), `expected the page's surface colour: ${handle}`);
    ok(/var\(--accent\)/.test(handle), `expected the page's accent colour: ${handle}`);
    ok(/box-shadow/.test(handle), `expected the handles to lift off the map tiles: ${handle}`);
  });

  // =======================================================================
  // Task 38, item 3: the log lives under the map, and the form pane runs
  // the full height beside it.
  //
  // Layout, so these are assertions about the committed markup and the
  // committed stylesheet: this harness models elements by id with no tree
  // between them and cannot measure a box. What can be pinned is the
  // structure the claim rests on, which is that the log is inside the map
  // pane, that there is one of it, and that everything Task 31 and Task
  // 36 put somewhere is still where they put it.
  // =======================================================================

  const MAP_PANE_MARKUP = INDEX_HTML_MARKUP.slice(
    INDEX_HTML_MARKUP.indexOf('class="map-pane"'),
    INDEX_HTML_MARKUP.indexOf('class="form-pane"')
  );

  await test("the log is the last thing in the map's own column, and there is one of it", () => {
    ok(MAP_PANE_MARKUP.length > 0, "expected to have found the map pane's markup to check");
    ok(MAP_PANE_MARKUP.includes('id="log"'), "expected the log inside the map pane");
    const logs = INDEX_HTML_MARKUP.match(/id="log"/g) || [];
    ok(logs.length === 1, `expected exactly one log, found ${logs.length}: it was moved, not copied`);
    ok(!/<footer>/.test(INDEX_HTML_MARKUP), "expected the full-width footer gone with the move");
    // Last, under the map's own furniture rather than between the map and
    // the strip that reports on it.
    ok(
      MAP_PANE_MARKUP.indexOf('class="map-tools"') < MAP_PANE_MARKUP.indexOf('id="log"'),
      "expected the log below the map tools row"
    );
  });

  await test("the strip and the tools row are still where Task 36 put them", () => {
    // The log arriving in this column must not have pushed anything else
    // around inside it.
    const map = MAP_PANE_MARKUP.indexOf('id="map"');
    const strip = MAP_PANE_MARKUP.indexOf('id="map-status"');
    const tools = MAP_PANE_MARKUP.indexOf('class="map-tools"');
    ok(map !== -1 && strip !== -1 && tools !== -1, "expected all three still in the map pane");
    ok(map < strip && strip < tools, "expected map, then the legend/progress strip, then the tools");
  });

  await test("the form pane is still the second column of a two-column main", () => {
    // "leave the entire right tab running all the way down" is a
    // consequence of the log leaving <main>, not of a new rule: main is
    // still the same two-column grid, and a grid column stretches.
    const main = cssRule("main");
    // Anchored on the semicolon: without it a third column added on the
    // end still matches the two this is meant to be pinning.
    ok(/grid-template-columns:\s*1fr 340px;/.test(main), `expected the two columns unchanged: ${main}`);
    ok(/min-height:\s*0/.test(main), `expected main still able to shrink: ${main}`);
    ok(
      INDEX_HTML_MARKUP.indexOf('class="map-pane"') < INDEX_HTML_MARKUP.indexOf('class="form-pane"'),
      "expected the map column first and the form column second"
    );
    // The log holds the height it is given rather than sharing out what
    // is left with the map beside it.
    const log = cssRule(".log");
    ok(/flex:\s*none/.test(log), `expected the log to hold its own height: ${log}`);
    ok(/overflow-y:\s*auto/.test(log), `expected the log still scrollable: ${log}`);
  });

  await test("the settings panel is still an overlay, not a third column", () => {
    const panelAt = INDEX_HTML_MARKUP.indexOf('<div id="settings-panel"');
    const mainEnds = INDEX_HTML_MARKUP.indexOf("</main>");
    ok(panelAt !== -1 && mainEnds !== -1, "expected both the panel and the end of main");
    ok(panelAt > mainEnds, "the settings panel must stay outside the grid, or it becomes a column");
    const panel = cssRule(".settings-panel");
    ok(/position:\s*fixed/.test(panel), `expected the panel still fixed over the page: ${panel}`);
  });

  await test("the form pane still holds everything the last two tasks put in it", () => {
    // The destination box above Download, the folder preview above that,
    // Task 27's overrun note and Task 31's failure list. All of them are
    // in the column that just changed height, so all of them are worth a
    // line here.
    const formPane = INDEX_HTML_MARKUP.slice(INDEX_HTML_MARKUP.indexOf('class="form-pane"'));
    for (const id of [
      "estimate",
      "folder-preview",
      "output-root",
      "output-root-browse",
      "download",
      "progress-note",
      "tile-failures",
    ]) {
      ok(formPane.includes(`id="${id}"`), `expected id="${id}" still in the form pane`);
    }
    // And the progress bar is still on the strip under the map rather
    // than having drifted back into the form with the rest.
    ok(!formPane.includes('id="progress"'), "the progress bar belongs to the strip under the map");
  });

  await test("the log still takes the job's own lines after the move", async () => {
    // The one behavioural half of a layout change: log() writes by id, so
    // this proves the id it writes to is the element that moved rather
    // than a second one left behind.
    const tileIds = tileIdsUpTo(2);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [
        {
          state: "done",
          events: [
            ...tileIds.map((tileId) => ({ event: "tile_done", source: "osm", tile_id: tileId })),
            { event: "source_done", source: "osm" },
          ],
        },
      ],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    const lines = sandbox.document.getElementById("log").children.map((line) => line.textContent);
    ok(lines.length > 0, "expected the run's events in the log");
    ok(lines.some((line) => line.startsWith("tile_done")), `got ${JSON.stringify(lines)}`);
  });

  // =======================================================================
  // Task 38, item 4: a draggable divider between the map and the log.
  //
  // The split is the log's height: the map beside it is flex: 1, so
  // whatever the log takes the map gives up. Nothing here can lay a page
  // out, so the two heights the arithmetic works from are supplied the
  // way a browser would have measured them, and what is checked is the
  // arithmetic, the floors, the call that keeps Leaflet's tiles honest,
  // and what is remembered.
  // =======================================================================

  // A page whose layout has happened: a 400px map above a 120px log, so
  // the two of them share 520px.
  function layOutColumn(sandbox, { map = 400, log = 120 } = {}) {
    sandbox.document.getElementById("map").offsetHeight = map;
    sandbox.document.getElementById("log").offsetHeight = log;
  }

  // The split is a saved setting like the theme and the tile size, so a
  // sandbox that is going to drag the divider needs the route every
  // setting is written through. Returns what actually reached the server,
  // parsed, which is what "saved once at the end and not sixty times on
  // the way" has to be asserted against.
  async function dividerSandbox(configOverrides, layout) {
    const puts = [];
    const config = { ...DEFAULT_CONFIG, ...(configOverrides || {}) };
    const { sandbox } = await bootedSandbox(
      async (url, options) => {
        if (url.pathname !== "/api/config") return null;
        const method = (options.method || "GET").toUpperCase();
        if (method === "GET") return jsonResponse(200, config);
        if (method === "PUT") {
          const sent = JSON.parse(options.body);
          puts.push(sent);
          Object.assign(config, sent);
          return jsonResponse(200, config);
        }
        return null;
      },
      undefined,
      (built) => {
        // Before app.js runs, because boot() restores the saved split
        // against these the moment the config lands.
        built.document.getElementById("map").offsetHeight = (layout || {}).map || 400;
        built.document.getElementById("log").offsetHeight = (layout || {}).log || 120;
      }
    );
    return { sandbox, puts };
  }

  function logHeightPuts(puts) {
    return puts.filter((sent) => "log_height_px" in sent);
  }

  function dragDivider(sandbox, fromY, toY, { release = true } = {}) {
    sandbox.document.getElementById("log-resizer").fire("mousedown", { clientY: fromY });
    sandbox.document.fire("mousemove", { clientY: toY });
    if (release) sandbox.document.fire("mouseup", {});
  }

  function logStyleHeight(sandbox) {
    return parseFloat(String(sandbox.document.getElementById("log").style.height || ""));
  }

  await test("the divider sits between the map's furniture and the log", () => {
    const tools = MAP_PANE_MARKUP.indexOf('class="map-tools"');
    const resizer = MAP_PANE_MARKUP.indexOf('id="log-resizer"');
    const log = MAP_PANE_MARKUP.indexOf('id="log"');
    ok(resizer !== -1, "expected the divider in the map pane");
    ok(tools < resizer && resizer < log, "expected the divider directly above the log");
    const resizers = INDEX_HTML_MARKUP.match(/id="log-resizer"/g) || [];
    ok(resizers.length === 1, `expected exactly one divider, found ${resizers.length}`);
    const rule = cssRule(".log-resizer");
    ok(/cursor:\s*row-resize/.test(rule), `expected the splitter cursor: ${rule}`);
    ok(/flex:\s*none/.test(rule), `expected the divider to hold its own height: ${rule}`);
  });

  await test("dragging the divider up gives the log the height the map loses", async () => {
    const { sandbox } = await bootedSandbox();
    layOutColumn(sandbox);
    const before = sandbox.L._mapObject._invalidateSizeCalls;
    dragDivider(sandbox, 500, 400); // 100px up the screen
    ok(logStyleHeight(sandbox) === 220, `expected 120 + 100, got ${logStyleHeight(sandbox)}`);
    ok(
      sandbox.L._mapObject._invalidateSizeCalls > before,
      "Leaflet was never told its box changed: the tiles would be laid out for the old height"
    );
  });

  await test("dragging the divider down gives it back", async () => {
    const { sandbox } = await bootedSandbox();
    layOutColumn(sandbox);
    dragDivider(sandbox, 500, 540); // 40px down the screen
    ok(logStyleHeight(sandbox) === 80, `expected 120 - 40, got ${logStyleHeight(sandbox)}`);
  });

  await test("neither the log nor the map can be crushed to nothing", async () => {
    const { sandbox } = await bootedSandbox();
    layOutColumn(sandbox);
    // All the way up: the map keeps its own floor out of the 520 they
    // share, so the log stops at 360 rather than taking the lot.
    dragDivider(sandbox, 500, -2000);
    ok(logStyleHeight(sandbox) === 360, `expected the map's 160px floor kept, got ${logStyleHeight(sandbox)}`);
    // And all the way down, where the log's own floor holds.
    layOutColumn(sandbox, { map: 40, log: 360 });
    dragDivider(sandbox, 500, 3000);
    ok(logStyleHeight(sandbox) === 60, `expected the log's own floor, got ${logStyleHeight(sandbox)}`);
  });

  await test("the split is saved when the drag ends, and not before", async () => {
    // Once, at the end, through the same PUT /api/config every other
    // setting on this page goes through. Sixty saves on the way to one
    // choice would be sixty writes of config.json.
    const { sandbox, puts } = await dividerSandbox();
    dragDivider(sandbox, 500, 420, { release: false });
    await flush(10);
    ok(logHeightPuts(puts).length === 0, `expected nothing saved mid-drag, got ${JSON.stringify(puts)}`);
    sandbox.document.fire("mouseup", {});
    await flush(10);
    const saved = logHeightPuts(puts);
    ok(saved.length === 1, `expected exactly one save on the release, got ${JSON.stringify(puts)}`);
    ok(
      saved[0].log_height_px === 200,
      `expected the chosen split saved, got ${JSON.stringify(saved[0])}`
    );
    // A number, not the string an input's value would have given: the
    // endpoint applies what it is handed with no type check of its own,
    // and load_config would refuse a string on the next launch and fall
    // back to the default with nothing on screen to say why.
    ok(
      typeof saved[0].log_height_px === "number",
      `expected a real number, got ${typeof saved[0].log_height_px}`
    );
  });

  await test("a saved split is applied from the config the page boots with", async () => {
    // The relaunch case, which is the one localStorage could not do: this
    // comes back from config.json through GET /api/config, so it survives
    // the server being restarted on a different port.
    const { sandbox } = await dividerSandbox({ log_height_px: 240 });
    ok(logStyleHeight(sandbox) === 240, `expected the saved split, got ${logStyleHeight(sandbox)}`);
  });

  await test("a saved split too tall for this window is cut down to fit it", async () => {
    // Saved on a big monitor, opened on a laptop. The map keeps its floor
    // rather than the page opening with no map at all.
    const { sandbox } = await dividerSandbox({ log_height_px: 5000 });
    ok(logStyleHeight(sandbox) === 360, `expected it clamped to fit, got ${logStyleHeight(sandbox)}`);
  });

  await test("a config with nothing saved leaves the log exactly as the stylesheet drew it", async () => {
    // 0 is Config.log_height_px's own default and means "never chosen",
    // not "no height".
    const { sandbox } = await dividerSandbox({ log_height_px: 0 });
    ok(
      !sandbox.document.getElementById("log").style.height,
      `expected no height forced on a first visit, got ${sandbox.document.getElementById("log").style.height}`
    );
    ok(
      sandbox.L._mapObject._invalidateSizeCalls === 0,
      "expected no resize reported for a box that never changed"
    );
  });

  await test("a config load that fails leaves the divider at the stylesheet's height", async () => {
    // A stale token, or a server that has stopped: boot() throws on its
    // first await and says so in the log. The split goes with the output
    // root, the tile size and the theme, and the page opens at the
    // stylesheet's own 120px.
    //
    // And nothing is written back. savedConfig is still null, which is
    // what persistConfig refuses to write past, so a session that could
    // not READ the file cannot overwrite it with a value derived from
    // defaults.
    const puts = [];
    const { sandbox } = await bootedSandbox(async (url, options) => {
      if (url.pathname !== "/api/config") return null;
      if ((options.method || "GET").toUpperCase() === "PUT") {
        puts.push(JSON.parse(options.body));
        return jsonResponse(200, DEFAULT_CONFIG);
      }
      return jsonResponse(403, { error: "Bad token." });
    });
    layOutColumn(sandbox);
    ok(
      !sandbox.document.getElementById("log").style.height,
      `expected the stylesheet's height left alone, got ${sandbox.document.getElementById("log").style.height}`
    );
    dragDivider(sandbox, 500, 400);
    await flush(10);
    ok(logStyleHeight(sandbox) === 220, "the divider still works in a session whose config never loaded");
    ok(
      logHeightPuts(puts).length === 0,
      `a page that could not read the config must not write to it: ${JSON.stringify(puts)}`
    );
  });

  await test("a save the server refuses leaves the page working", async () => {
    // persistConfig already swallows a failed save for every other
    // setting, because it costs the next launch and not this session.
    // What must not happen is the drag itself coming apart.
    const rejections = [];
    const onRejection = (reason) => rejections.push(reason);
    process.on("unhandledRejection", onRejection);
    try {
      const { sandbox } = await bootedSandbox(async (url, options) => {
        if (url.pathname !== "/api/config") return null;
        if ((options.method || "GET").toUpperCase() === "PUT") {
          return jsonResponse(500, { error: "Disk full." });
        }
        return jsonResponse(200, DEFAULT_CONFIG);
      });
      layOutColumn(sandbox);
      dragDivider(sandbox, 500, 400);
      await flush(20);
      ok(logStyleHeight(sandbox) === 220, "the split the owner chose is still on screen");
      // And a second drag still works, so nothing was left in a state
      // the first failure poisoned.
      dragDivider(sandbox, 500, 450);
      await flush(20);
      ok(logStyleHeight(sandbox) === 270, `expected the second drag to work too, got ${logStyleHeight(sandbox)}`);
      ok(rejections.length === 0, `expected no unhandled rejection, got ${rejections[0]}`);
    } finally {
      process.off("unhandledRejection", onRejection);
    }
  });

  await test("a mouse moving with no drag in progress moves nothing", async () => {
    const { sandbox, puts } = await dividerSandbox();
    sandbox.document.fire("mousemove", { clientY: 10 });
    sandbox.document.fire("mouseup", {});
    await flush(10);
    ok(
      !sandbox.document.getElementById("log").style.height,
      `expected the log untouched by an ordinary mouse move, got ${sandbox.document.getElementById("log").style.height}`
    );
    ok(logHeightPuts(puts).length === 0, "expected an ordinary mouseup to save nothing");
  });

  // =======================================================================
  // Task 38, item 5: shorter, more concrete progress text.
  //
  // The rest of it is checked where the lines it replaced were checked,
  // beside the bar and the countdown above. What is left here is the
  // shape of the tile half of the line and the one thing that could go
  // wrong quietly: the package name being read from wherever the page has
  // got to rather than from the run that is actually going.
  // =======================================================================

  await test("a layer with no tile of the plan at all never names one", async () => {
    // Elevation downloads the whole extent and reports it under
    // "whole-area", which is not a tile of the plan. It has to reach the
    // status line as the layer's name, and it must not reach it as a
    // tile id the grid has never heard of.
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00", "r00_c01"],
      ["elevation"],
      [{ event: "tile_done", source: "elevation", tile_id: "whole-area" }],
      true
    );
    ok(summary.phase.kind === "downloading", JSON.stringify(summary.phase));
    ok(summary.phase.tile === "", `expected no tile named, got ${JSON.stringify(summary.phase)}`);
    ok(
      sandbox.phaseLabel(summary.phase) === "Downloading elevation",
      sandbox.phaseLabel(summary.phase)
    );
  });

  await test("a retry names the tile it has gone back for", async () => {
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00", "r00_c01"],
      ["osm"],
      [
        { event: "tile_failed", source: "osm", tile_id: "r00_c01", kind: "timeout", reason: "x" },
        { event: "tile_retrying", source: "osm", tile_id: "r00_c01", pass_number: 1, of: 2 },
      ],
      true
    );
    ok(sandbox.phaseLabel(summary.phase) === "Retrying osm r00_c01", sandbox.phaseLabel(summary.phase));
  });

  await test("the package named is the one this run is building, not the one the page is on", async () => {
    // The owner is free to start drawing the next survey while this one
    // downloads. The name on the line is snapshotted when Download is
    // pressed, for the same reason the estimate the bar weighs itself by
    // is: it describes the job in flight, not the form.
    const tileIds = tileIdsUpTo(4);
    const { sandbox } = await jobSandbox({
      tileIds,
      sources: [TWO_SOURCES[0]],
      sourceSeconds: [{ id: "osm", seconds_estimate: 144 }],
      polls: [{ state: "running", events: [{ event: "tile_done", source: "osm", tile_id: tileIds[0] }] }],
    });
    sandbox.document.getElementById("download").fire("click");
    await flush(900);
    ok(
      sandbox.document.getElementById("progress-status").textContent === "osm r00_c00 · out",
      sandbox.document.getElementById("progress-status").textContent
    );
    // The page loses the folder it knew: a degenerate extent is refused,
    // which clears the preview and everything derived from it.
    setField(sandbox, "bbox", "10,20,10,25");
    await flush(10);
    ok(
      sandbox.document.getElementById("folder-preview").hidden === true,
      "expected the preview cleared by the rejected extent"
    );
    ok(
      sandbox.progressStatusLine({ kind: "checking", source: "" }, { result_root: null }) ===
        "Checking the files · out",
      `expected the running job's own package still named, got ${sandbox.progressStatusLine(
        { kind: "checking", source: "" },
        { result_root: null }
      )}`
    );
  });

  console.log(
    `\n${failures === 0 ? `ALL ${passed} CHECKS PASSED` : failures + " CHECK(S) FAILED: " + failedNames.join(", ")}`
  );
  process.exit(failures === 0 ? 0 : 1);
})();
