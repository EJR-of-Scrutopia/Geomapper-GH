# Phase 2b Item C: Per-Source Detail Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before Download, the tier list tells the owner what quality each source delivers for the exact extent they drew: computed for the Wales LiDAR (1 m or an overview level, from the same arithmetic the real download uses), fixed honest strings for everything else, recorded in survey.json.

**Architecture:** A third optional LayerSource extension beside covers/tier: `detail(bbox) -> str | None`, disk-and-arithmetic only, never network. The resolver copies it into each resolution entry; the UI appends the base entry's detail to each category line; survey.json carries it through the existing shared resolve() call.

**Tech Stack:** Python stdlib; vanilla JS in app.js; no new dependencies, no build step.

## Global Constraints

- `detail()` never touches the network (poisoned-seam tests, like covers()).
- Copy strings are EXACT (listed per task below); no em dashes anywhere: code, comments, docs, UI copy, commit messages.
- No AI attribution anywhere. Explicit-path `git add`, never -A.
- No new third-party dependencies. No URL in errors.
- The spec (docs/superpowers/specs/2026-08-07-mapgen-phase2b-addendum-design.md, Item C) governs; the strings below are copied from it.
- Suite baselines at plan time: Python 1745 passed / 16 deselected, Node 281 (`node tests/js/test_app.js`).

## File Structure

- Modify: `src/mapgen/sources/lidar_wales.py` (level-aware `_pixels_per_raster`, `detail()`)
- Modify: `src/mapgen/sources/osm.py`, `overture.py`, `elevation.py`, `inspire.py`, `os_open.py`, `os_uprn.py` (static `detail()`)
- Modify: `src/mapgen/sources/base.py` (document the extension)
- Modify: `src/mapgen/resolver.py` (entries gain `detail`)
- Modify: `src/mapgen/web/static/app.js` (+ `tests/js/test_app.js`)
- Modify: `tests/test_lidar_wales.py`, `tests/test_resolver.py`, `tests/test_web_server.py`, `tests/test_package.py`
- Modify: `README.md`, `docs/superpowers/HANDOFF.md`

---

### Task 1: the computed LiDAR detail

**Files:**
- Modify: `src/mapgen/sources/lidar_wales.py` (`_pixels_per_raster` at ~line 255, `estimate` call site at ~line 371, new `detail`)
- Test: `tests/test_lidar_wales.py`

**Interfaces:**
- Consumes: `_pixels_per_raster(padded_area_m2, max_pixels)` currently returning `(pixels, is_overview)`; `padded_bng_extent` + the pad constant the source already uses; `covers(bbox)` (Task 7 of item 3).
- Produces: `_pixels_per_raster` returns `(pixels, level)` where level 0 is full resolution and each increment quarters the pixel count (`is_overview` at the call sites becomes `level > 0`); `detail(bbox) -> str | None`.

**Copy (exact):**
- Level 0: `1 m at this extent`
- Level >= 1: `{2**level} m at this extent (extents under about 4 x 4 km come back at 1 m)` where `{2**level}` renders as a plain int (2, 4, 8...).
- `covers(bbox) == "none"`: return None (a source with no coverage here has no detail to claim).

**Steps:**

- [ ] **Step 1: Failing tests.** (a) `_pixels_per_raster(1_000_000.0)` returns level 0; `_pixels_per_raster(20_500_000.0)` returns level 1 (Port Talbot's real case); `_pixels_per_raster(4.0 * MAX_WINDOW_PIXELS + 1)` returns level 2; existing is_overview assertions rewritten against `level`. (b) `detail()` over a small Vale extent (reuse an existing in-mosaic fixture bbox from this test file) == `"1 m at this extent"`; over a Port-Talbot-sized bbox (build one ~6.7 x 3.1 km inside the mosaic) starts with `"2 m at this extent"`; over a Paris bbox returns None. (c) a poisoned-opener fixture proves no network (mirror the covers() no-network test pattern already in this file).
- [ ] **Step 2: Run, confirm failures.** `pytest tests/test_lidar_wales.py -v` shows the new tests RED (attribute missing / tuple shape).
- [ ] **Step 3: Implement.** Change `_pixels_per_raster`'s loop to count levels; update `estimate`'s unpacking (`is_overview = level > 0`, behaviour identical); add `detail(bbox)`: covers "none" -> None, else `padded_bng_extent` area -> `_pixels_per_raster` -> the copy strings above.
- [ ] **Step 4: Green, full module suite, then full Python suite.**
- [ ] **Step 5: Commit** `feat(detail): lidar level preview from the estimate's own walk`.

### Task 2: static details, resolver field, survey.json

**Files:**
- Modify: the six other source modules, `sources/base.py`, `src/mapgen/resolver.py`
- Test: `tests/test_resolver.py`, `tests/test_web_server.py`, `tests/test_package.py`

**Interfaces:**
- Consumes: `resolve(bbox, sources)` (item 3 Task 7); the optional-extension getattr convention.
- Produces: resolution entries gain `"detail": <str>` ONLY when the source has a detail() returning non-None (absent key otherwise, keeping the payload JSON-lean); every entry shape stays otherwise identical so Task 8's renderer keeps working before Task 3 lands.

**Copy (exact, one static string per source, returned regardless of bbox except where noted):**
- elevation: `30 m (Copernicus GLO-30)`
- os_open: `1:10,000 scale, generalized footprints (OS OpenMap Local)`
- osm: `traced footprints and centrelines, typically 1 to 5 m positional accuracy`
- overture: `traced footprints and centrelines, typically 1 to 5 m positional accuracy`
- inspire: `indicative extents, not legal boundaries`
- os_uprn: `one point per addressable location`

**Steps:**

- [ ] **Step 1: Failing tests.** resolver: a Cardiff resolution's terrain entries carry detail for lidar_wales (starts "1 m" or "2 m" depending on the test bbox size) and elevation ("30 m (Copernicus GLO-30)"); a stub source without detail() produces entries WITHOUT the key; json.dumps round-trip still passes. web server: the estimate payload's resolution entries carry detail (extend the existing resolution payload test). package: survey.json's recorded resolution carries the same (extend the existing stub-with-extensions case with a detail()).
- [ ] **Step 2: RED run.**
- [ ] **Step 3: Implement.** One-line `detail(self, bbox)` returning the constant per source (document in each that the string is spec copy); base.py docstring gains the extension paragraph beside covers/tier; resolver's entry assembly adds the key defensively.
- [ ] **Step 4: Green, full Python suite.**
- [ ] **Step 5: Commit** `feat(detail): static source details into the resolution record`.

### Task 3: the UI line, and docs

**Files:**
- Modify: `src/mapgen/web/static/app.js`
- Test: `tests/js/test_app.js`
- Modify: `README.md` (resolution field-table row mentions detail), `docs/superpowers/HANDOFF.md` (item C shipped; next = item A)

**Interfaces:**
- Consumes: the payload shape from Task 2; `renderResolution` and its copy rules (item 3 Task 8).
- Produces: each category line appends the BASE entry's detail after the base name, comma-separated: `terrain: LiDAR terrain (Wales, 1 m), 2 m at this extent (extents under about 4 x 4 km come back at 1 m), filled by Elevation (Copernicus GLO-30)`. Fill/reference entries' details stay payload-only (rendering every entry's detail would triple the line; the base's quality is what the package gets). Entries without detail render exactly as today.

**Steps:**

- [ ] **Step 1: Read the existing renderResolution tests, write failing Node tests.** Base-with-detail renders the comma-joined form; base-without-detail unchanged; a fill entry's detail does NOT render; escaping still holds with a hostile detail string.
- [ ] **Step 2: RED run** (`node tests/js/test_app.js`).
- [ ] **Step 3: Implement, green, full Node + full Python suites.**
- [ ] **Step 4: Docs.** README's resolution row documents the optional detail field and where it renders; HANDOFF marks item C shipped and points at item A (categorised boundaries). No em dashes.
- [ ] **Step 5: Commit** `feat(web): detail preview on the tier list`.

## Self-review notes

- Spec coverage: Item C's computed lidar string, six static strings, payload + survey.json + UI line, no-network rule: all tasked. lidar_cardiff's string belongs to item D, correctly absent here.
- Type consistency: `(pixels, level)` tuple change is contained to lidar_wales (grep confirms `_pixels_per_raster` has no callers outside the module).
- Placeholders: none; every string is spelled out.
