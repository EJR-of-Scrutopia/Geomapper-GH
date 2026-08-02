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

function makeElement(id) {
  return {
    id,
    value: "",
    checked: false,
    disabled: false,
    hidden: false,
    className: "",
    textContent: "",
    innerHTML: "",
    style: {},
    scrollHeight: 0,
    scrollTop: 0,
    children: [],
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
    appendChild(node) {
      this.children.push(node);
    },
  };
}

function makeDocument() {
  const elements = new Map();
  const listeners = {};
  return {
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
    querySelectorAll() {
      return [];
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
  { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false },
];

function bootRoutes(extra) {
  return (url, options, call) => {
    if (url.pathname === "/api/config" && (!options.method || options.method === "GET")) {
      return jsonResponse(200, DEFAULT_CONFIG);
    }
    if (url.pathname === "/api/sources") {
      return jsonResponse(200, DEFAULT_SOURCES);
    }
    return extra ? extra(url, options, call) : null;
  };
}

function buildSandbox({ fetch, token = DEFAULT_TOKEN }) {
  const document = makeDocument();
  const sandbox = {
    location: { search: `?token=${token}`, origin: FETCH_ORIGIN },
    document,
    L: makeLeaflet(),
    fetch,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    URL,
    URLSearchParams,
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
  await flush(10); // let boot()'s two awaits settle
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
      if (url.pathname === "/api/sources") {
        return jsonResponse(200, [
          { id: "osm", display_name: "OpenStreetMap", licence: "ODbL", requires_api_key: false },
          { id: "overture", display_name: "Overture Maps", licence: "ODbL", requires_api_key: false },
          {
            id: "elevation",
            display_name: "Elevation (OpenTopography COP30)",
            licence: "Copernicus DEM",
            requires_api_key: true,
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

  await test("boot() pre-fills the API key field from the saved config", async () => {
    const stub = makeFetchStub(async (url) => {
      if (url.pathname === "/api/config") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-saved-key" });
      }
      if (url.pathname === "/api/sources") return jsonResponse(200, DEFAULT_SOURCES);
      return null;
    });
    const sandbox = buildSandbox({ fetch: stub.fetch });
    await flush(10);
    ok(sandbox.document.getElementById("opentopo-key").value === "sk-saved-key");
  });

  await test("the API key persists immediately on change, with no estimate to validate it against", async () => {
    const { fetchCalls, sandbox } = await bootedSandbox((url, options) => {
      if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
        return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-new-key" });
      }
      return null;
    });
    fetchCalls.length = 0;
    setField(sandbox, "opentopo-key", "sk-new-key");
    await flush(10);
    const putCall = fetchCalls.find((c) => (c.options.method || "").toUpperCase() === "PUT");
    ok(putCall, "expected an immediate PUT /api/config call");
    const body = JSON.parse(putCall.options.body);
    ok(body.opentopography_api_key === "sk-new-key", `unexpected PUT body: ${putCall.options.body}`);
  });

  await test(
    "the API key is saved before the next estimate re-checks readiness, not raced against it",
    async () => {
      // Review round 1: refreshEstimate and the persist handler used to
      // be two independent "change" listeners on the same field. Both
      // fire, but the server's own readiness check reads the SAVED
      // config file, not this field's live value, so the estimate
      // dispatched by refreshEstimate could reach the server before the
      // PUT did, reporting "no key configured" for a key just typed in.
      const callOrder = [];
      const { sandbox } = await bootedSandbox((url, options) => {
        if (url.pathname === "/api/config" && (options.method || "").toUpperCase() === "PUT") {
          callOrder.push("PUT config");
          return jsonResponse(200, { ...DEFAULT_CONFIG, opentopography_api_key: "sk-new-key" });
        }
        if (url.pathname === "/api/estimate") {
          callOrder.push("estimate");
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
      callOrder.length = 0;
      setField(sandbox, "opentopo-key", "sk-new-key");
      await flush(10);
      ok(callOrder.includes("PUT config"), `expected a PUT config call, got: ${callOrder}`);
      ok(callOrder.includes("estimate"), `expected a follow-up estimate call, got: ${callOrder}`);
      ok(
        callOrder.indexOf("PUT config") < callOrder.indexOf("estimate"),
        `expected the key saved before the next estimate, got order: ${callOrder}`
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
    setField(sandbox, "opentopo-key", SECRET);
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

  console.log(
    `\n${failures === 0 ? `ALL ${passed} CHECKS PASSED` : failures + " CHECK(S) FAILED: " + failedNames.join(", ")}`
  );
  process.exit(failures === 0 ? 0 : 1);
})();
