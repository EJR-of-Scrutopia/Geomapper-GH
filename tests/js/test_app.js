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
const KNOWN_IDS = new Set(
  Array.from(fs.readFileSync(INDEX_HTML_PATH, "utf8").matchAll(/\bid="([^"]+)"/g), (m) => m[1])
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
  // Attributes set through setAttribute, kept apart from the plain
  // properties above because they are not the same thing: app.js's
  // progress bar (Task 27) writes aria-valuenow here, which no property
  // on a real element mirrors, and a test that could only read
  // textContent would be unable to tell whether a screen reader was
  // being told the same number the bar was drawing.
  const attributes = {};
  const element = {
    id,
    get value() {
      return value;
    },
    set value(next) {
      if (optionValues.length && !optionValues.includes(String(next))) {
        value = "";
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

  const mapObject = {
    setView() {
      return this;
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
    removeLayer(layer) {
      if (layer) layer.removed = true;
    },
  };

  return {
    map: () => mapObject,
    tileLayer: () => ({
      addTo() {
        return this;
      },
    }),
    rectangle: (bounds, options) => {
      const handle = {
        bounds,
        options,
        removed: false,
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
    // Test-only hooks, not part of the real Leaflet API: every rectangle
    // ever created in this sandbox in creation order, and the map object
    // itself, so a test can drive map.fire(...) and inspect what setBBox
    // and the draw tool's preview actually did with it.
    _rectangles: rectangles,
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

function buildSandbox({ fetch, token = DEFAULT_TOKEN }) {
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
  vm.runInContext(SOURCE, sandbox, { filename: "app.js" });
  return sandbox;
}

function flush(ms = 0) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function bootedSandbox(extraRoutes, token) {
  const stub = makeFetchStub(bootRoutes(extraRoutes));
  const sandbox = buildSandbox({ fetch: stub.fetch, token });
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

async function test(name, fn) {
  try {
    await fn();
    passed += 1;
    console.log(`PASS  ${name}`);
  } catch (error) {
    failures += 1;
    failedNames.push(name);
    console.log(`FAIL  ${name}`);
    console.log(`      ${error.stack ? error.stack.split("\n").slice(0, 2).join("\n      ") : error}`);
  }
}

function ok(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

(async () => {
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
  // Task 18, item 3: the rubber-band draw tool. Armed state, the click-
  // move-click sequence, the preview rectangle's lifecycle, and Escape
  // leaving any previous extent untouched.
  // =======================================================================

  await test("pressing Draw extent arms the button with a visible state", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    const button = sandbox.document.getElementById("draw");
    ok(button.className === "armed", `expected the armed class, got ${JSON.stringify(button.className)}`);
    ok(/corner/i.test(button.textContent), `expected the label to mention a corner, got ${button.textContent}`);
  });

  await test("clicking two corners on the map commits the extent and disarms", async () => {
    const { sandbox } = await bootedSandbox();
    sandbox.document.getElementById("draw").fire("click");
    sandbox.L._mapObject.fire("click", { latlng: { lat: 51.4, lng: -3.3 } });
    ok(sandbox.document.getElementById("draw").className === "armed", "expected still armed after one corner");
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.L._rectangles.some((r) => !r.removed), "expected a live preview rectangle after moving the cursor");
    sandbox.L._mapObject.fire("click", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.document.getElementById("draw").className === "", "expected disarmed after the second click");
    ok(sandbox.document.getElementById("draw").textContent === "Draw extent");
    ok(
      sandbox.document.getElementById("bbox").value === "-3.3,51.4,-3.28,51.42",
      `expected the committed bbox, got ${sandbox.document.getElementById("bbox").value}`
    );
  });

  await test(
    "the preview rectangle is reused across mousemoves and removed once the drag completes",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.document.getElementById("draw").fire("click");
      sandbox.L._mapObject.fire("click", { latlng: { lat: 51.4, lng: -3.3 } });
      sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.41, lng: -3.29 } });
      sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
      ok(
        sandbox.L._rectangles.length === 1,
        `expected exactly one rectangle created across two mousemoves, got ${sandbox.L._rectangles.length}`
      );
      ok(sandbox.L._rectangles[0].removed === false);
      sandbox.L._mapObject.fire("click", { latlng: { lat: 51.42, lng: -3.28 } });
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
    sandbox.L._mapObject.fire("click", { latlng: { lat: 51.5, lng: -3.1 } });
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
    sandbox.L._mapObject.fire("click", { latlng: { lat: 51.4, lng: -3.3 } });
    sandbox.L._mapObject.fire("mousemove", { latlng: { lat: 51.42, lng: -3.28 } });
    ok(sandbox.document.getElementById("draw").textContent === "Click the opposite corner");
    sandbox.document.getElementById("draw").fire("click"); // pressed again instead of a second corner
    ok(
      sandbox.document.getElementById("draw").textContent === "Click a corner",
      "expected a fresh arm, waiting for a first corner again"
    );
    ok(sandbox.L._rectangles.every((r) => r.removed), "expected the stranded preview rectangle cleaned up");
    // Confirms the map is genuinely waiting for a FIRST corner again, not
    // carrying a leftover firstCorner from before: this click must be
    // treated as the first, not the second.
    sandbox.L._mapObject.fire("click", { latlng: { lat: 52.0, lng: -4.0 } });
    ok(sandbox.document.getElementById("draw").textContent === "Click the opposite corner");
  });

  // =======================================================================
  // Review round 1: a zero-area extent (a second click on the first
  // point, or a degenerate pasted bbox) must be rejected at the point of
  // entry, leaving any previously committed extent untouched, rather than
  // silently committing a degenerate box that only the server notices.
  // =======================================================================

  await test(
    "a second click on the same point is rejected, leaving the previous extent untouched",
    async () => {
      const { sandbox } = await bootedSandbox();
      setField(sandbox, "bbox", "-3.29,51.38,-3.28,51.39");
      const before = sandbox.document.getElementById("bbox").value;
      const rectanglesBefore = sandbox.L._rectangles.length;
      sandbox.document.getElementById("draw").fire("click");
      sandbox.L._mapObject.fire("click", { latlng: { lat: 51.5, lng: -3.1 } });
      sandbox.L._mapObject.fire("click", { latlng: { lat: 51.5, lng: -3.1 } }); // same point
      ok(
        sandbox.document.getElementById("bbox").value === before,
        `expected the previous extent untouched, got ${sandbox.document.getElementById("bbox").value}`
      );
      ok(sandbox.document.getElementById("draw").className === "", "expected disarmed, not stuck armed");
      ok(sandbox.document.getElementById("draw").textContent === "Draw extent");
      ok(
        sandbox.L._rectangles.length === rectanglesBefore,
        "expected no new committed rectangle from a degenerate click pair"
      );
    }
  );

  await test(
    "a zero-width click pair (same longitude, different latitude) is also rejected",
    async () => {
      const { sandbox } = await bootedSandbox();
      sandbox.document.getElementById("draw").fire("click");
      sandbox.L._mapObject.fire("click", { latlng: { lat: 51.4, lng: -3.3 } });
      sandbox.L._mapObject.fire("click", { latlng: { lat: 51.5, lng: -3.3 } }); // same lng only
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

  await test("classifyTiles: a failed tile stays failed even if a later event for it arrives", async () => {
    const { sandbox } = await bootedSandbox();
    const events = [
      { event: "tile_failed", tile_id: "r00_c00" },
      { event: "tile_done", tile_id: "r00_c00" }, // stale/late, must not un-fail it
      { event: "source_done", source: "osm" },
    ];
    const result = sandbox.classifyTiles(["r00_c00"], ["osm"], events, false);
    ok(result.get("r00_c00") === "failed", `expected failed to stick, got ${result.get("r00_c00")}`);
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

  await test("progress: subdivisions are counted from the events, once each", async () => {
    const { sandbox } = await bootedSandbox();
    const summary = sandbox.summariseJob(
      ["r00_c00", "r00_c01"],
      ["osm"],
      [
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c00", pieces: 4, depth: 1 },
        { event: "tile_subdivided", source: "osm", tile_id: "r00_c00_q02", pieces: 4, depth: 2 },
      ],
      true,
      BARRY_SECONDS
    );
    ok(summary.subdivisions === 2, `expected 2 subdivisions, got ${summary.subdivisions}`);
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
    ok(label.text.includes("from the estimate"), `expected the copy to say which branch it is on, got ${label.text}`);
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
    ok(label.text.includes("from the rate so far"), `expected the copy to name the branch, got ${label.text}`);
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
  async function jobSandbox({ tileIds, polls, sources = TWO_SOURCES, sourceSeconds }) {
    let pollIndex = 0;
    const { sandbox, fetchCalls } = await bootedSandbox((url, options) => {
      const method = (options.method || "GET").toUpperCase();
      if (url.pathname === "/api/sources") return jsonResponse(200, sources);
      if (url.pathname === "/api/config" && method === "PUT") return jsonResponse(200, DEFAULT_CONFIG);
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
      if (url.pathname === "/api/jobs" && method === "POST") return jsonResponse(202, { id: "job1" });
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
    ok(text.includes("50% done"), `expected the percentage in the copy, got ${text}`);
    ok(/left/.test(text), `expected a countdown, got ${text}`);
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

  console.log(
    `\n${failures === 0 ? `ALL ${passed} CHECKS PASSED` : failures + " CHECK(S) FAILED: " + failedNames.join(", ")}`
  );
  process.exit(failures === 0 ? 0 : 1);
})();
