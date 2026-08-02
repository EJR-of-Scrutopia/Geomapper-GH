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

const APP_JS_PATH = path.join(__dirname, "..", "..", "src", "mapgen", "web", "static", "app.js");
const SOURCE = fs.readFileSync(APP_JS_PATH, "utf8");

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
    fire(type) {
      for (const handler of this._listeners[type] || []) handler();
    },
    appendChild(node) {
      this.children.push(node);
    },
  };
}

function makeDocument() {
  const elements = new Map();
  return {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeElement(id));
      return elements.get(id);
    },
    createElement() {
      return makeElement("log-line");
    },
    querySelectorAll() {
      return [];
    },
  };
}

function makeLeaflet() {
  return {
    map: () => ({
      setView() {
        return this;
      },
      on() {},
      getContainer: () => ({ style: {} }),
      fitBounds() {},
      removeLayer() {},
    }),
    tileLayer: () => ({
      addTo() {
        return this;
      },
    }),
    rectangle: () => ({ addTo() {} }),
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
          return jsonResponse(200, { west: -3.31, south: 51.38, east: -3.25, north: 51.43 });
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
      ok(call.url.searchParams.get("lat") !== null, "lat missing or corrupted");
      ok(call.url.searchParams.get("lon") !== null, "lon missing or corrupted");
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
          return jsonResponse(200, { west: -3.31, south: 51.38, east: -3.25, north: 51.43 });
        }
        return null;
      });
      fetchCalls.length = 0;
      setField(sandbox, "place", "Bar");
      await flush(50);
      setField(sandbox, "place", "Barr");
      await flush(50);
      setField(sandbox, "place", "Barry");
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
        return jsonResponse(200, { west: -3.31, south: 51.38, east: -3.25, north: 51.43 });
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
      ok(pollCount >= 1, "expected at least one poll attempt");
      ok(sandbox.document.getElementById("cancel").hidden === true, "expected cancel to hide after the poll failed");
      ok(
        sandbox.document.getElementById("download").disabled === false,
        "expected download to re-enable after the poll failed"
      );
      const logLines = sandbox.document.getElementById("log").children;
      const failLine = logLines.find((l) => l.className === "fail" && /lost contact/i.test(l.textContent));
      ok(failLine, `expected a "lost contact" log line; got: ${logLines.map((l) => l.textContent).join(" | ")}`);
    }
  );

  console.log(
    `\n${failures === 0 ? `ALL ${passed} CHECKS PASSED` : failures + " CHECK(S) FAILED: " + failedNames.join(", ")}`
  );
  process.exit(failures === 0 ? 0 : 1);
})();
