# Phase 2, item 2: INSPIRE property boundaries as curves Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `inspire` layer source that downloads HM Land Registry INSPIRE Index Polygons for the authorities covering the drawn extent and delivers property boundaries as deduplicated curves: a standalone GeoJSON, and tagged ways fused into the `.osm`, exactly as the owner designed ("just the curve after the fact... as a separate key and value").

**Architecture:** One new source module (`sources/inspire.py`: authority index, month-stamped zip cache, cookie-session download, streaming GML parse), one new geometry module (`boundary_curves.py`: ring dedup, shared-edge dedup, edge chaining), one `package.py` post-step (`_fuse_boundaries_step`) following the heights-fusion pattern, plus two small carried-in items from the owner's release feedback (contour Z coordinates if Urbano honours them; the WGS84/ETRS89 epoch-shift investigation). Everything stdlib plus the existing `requests`.

**Tech Stack:** Python stdlib (`xml.etree.iterparse`, `zipfile`, `json`), `requests` (existing; its `Session` handles the service's cookie handshake), pytest with the repo's `live` marker convention.

**Spec:** `docs/superpowers/specs/2026-08-05-mapgen-phase2-design.md`, build order item 2. Evidence: task-zero report claim 4, plus the live probe below.

## Measured facts this plan is built on (probed live 2026-08-06)

- `https://use-land-property-data.service.gov.uk/datasets/inspire/download` answers **HTTP 302 to itself for a cookieless client** (an infinite loop to bare urllib) and **HTTP 200 with a cookie jar**: the first hit sets a session cookie, the redirect retries the same URL with it. `requests.Session` handles this without any special code; the download layer MUST use one session for the page and the zips, and a test MUST pin that a cookieless fetch is what fails, so nobody "simplifies" the session away.
- The page lists **318 authority zips**, URL shape `/datasets/inspire/download/<Name_With_Underscores>.zip`. Confirmed present: `Vale_of_Glamorgan_Council.zip`, `Cardiff_Council.zip`, `Bridgend_County_Borough_Council.zip`, `Merthyr_Tydfil_County_Borough_Council.zip`.
- `Bridgend_County_Borough_Council.zip`: **13,128,716 bytes** (Content-Type `application/force-download`), containing `INSPIRE Download Licence.pdf` (35,584 bytes) and `Land_Registry_Cadastral_Parcels.gml` (**81,445,491 bytes uncompressed, 69,506 parcels**). The ~6x zip ratio and the 81 MB single member are why the parser is `iterparse` with element clearing, never a whole-document parse.
- GML shape, verbatim from the live file: WFS 2.0 `wfs:FeatureCollection` with `numberMatched`/`numberReturned` attributes; namespace `LR="www.landregistry.gov.uk"` (note: no scheme, exactly that string); features are `wfs:member/LR:PREDEFINED`, geometry `LR:GEOMETRY/gml:Polygon` with `srsName="urn:ogc:def:crs:EPSG::27700"` and `srsDimension="2"`, rings as `gml:exterior/gml:LinearRing/gml:posList` (space-separated easting northing pairs; values carry 1 to 3 decimals). `gml:interior` rings are legal GML and must be handled (parsed and carried) even if rare. The timestamp on the collection (`2026-08-02T01:28:46Z`) matches the first-Sunday publication cadence task zero verified.
- Attribution (task zero, quoted from the live conditions): BOTH statements are required because mapgen uses the geometry, plus the conditions link. Verbatim, with `[year]` replaced by the publication year of the downloaded data:
  1. `This information is subject to Crown copyright and database rights [year] and is reproduced with the permission of HM Land Registry.`
  2. `The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and database rights [year] Ordnance Survey AC0000851063.`
  3. Link: `https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions`
- HMLR's own caveat, reused as the indicative wording everywhere boundaries appear: `The extent of the land contained in any registered title cannot be established from the INSPIRE Index Polygons.` Boundary-crossing parcels appear in BOTH neighbouring authorities' files by design; the dedup handles it.

## File structure

| File | Responsibility |
|---|---|
| `src/mapgen/sources/inspire.py` (new) | The LayerSource: authority index lookup, month-stamped zip cache in `~/.mapgen/inspire/`, cookie-session download, streaming parcel extraction to a work file, merge to the boundaries GeoJSON |
| `src/mapgen/boundary_curves.py` (new) | Pure geometry: duplicate-ring removal, shared-edge dedup, chaining unique edges into maximal polylines |
| `src/mapgen/package.py` (modify) | Register the source; `_fuse_boundaries_step` injecting curves into the `.osm`; survey.json `inspire_boundaries` block |
| `src/mapgen/contours.py` (modify, Task 5, conditional) | Z coordinate on contour vertices if the Urbano importer honours it |
| `tests/fixtures/inspire/` (new) | `authority_index.json` (name to WGS84 bbox for all 318), `make_authority_index.py` (the committed generator), a small real-parcel GML fixture + its extractor |
| `tests/test_inspire.py`, `tests/test_boundary_curves.py` (new) | Per-module suites; live tests marked `live` |

## Global Constraints

Carried unchanged from item 1, plus the item 2 specifics. Every task includes these.

- **No new third-party dependencies. No build step.** stdlib plus the existing `requests` only.
- **No em dashes anywhere.** No AI attribution anywhere, no Co-Authored-By.
- **Commit after every task with explicit paths on `git add`, never `-A`.**
- **No URL and no key ever enters a `TileFailure.reason` or an exception message that leaves a module** (compose from the shared vocabulary; this source needs no key).
- **Both attribution statements plus the conditions link, verbatim as above**, into survey.json provenance and README; the indicative caveat wherever boundaries are described.
- **Boundaries are indicative, never legal**: HMLR's own sentence, not a paraphrase.
- **Nothing fabricated**: a parcel that does not parse is counted and skipped, never guessed at.
- **Fusion adds, never overwrites**: existing ways in the `.osm` are untouched; injected ways use negative ids and are identifiable by their tags.
- **The `.osm` remains parseable by Urbano**: the fusion step follows `heights.py`'s atomic-rewrite pattern exactly.
- Live tests carry the `live` marker (deselected by default); everything else offline from committed fixtures.
- Streaming discipline: the 81 MB GML is never `ET.parse`d whole; `iterparse` with `elem.clear()` after each `wfs:member`, pinned by a test that fails if peak element count grows with input size (structure the parser so a counter can see it).
- Mutation-verification briefs carry this machine's three false-clean traps (pyc mtime bump, CRLF patterns, no `git checkout --` reverts).

---

### Task 1: the authority index

**Files:**
- Create: `tests/fixtures/inspire/make_authority_index.py`, `tests/fixtures/inspire/authority_index.json`
- Create: `src/mapgen/sources/inspire.py` (index loading + lookup only, this task)
- Test: `tests/test_inspire.py`

**Interfaces:**
- Produces: `authorities_for(bbox: BBox) -> list[str]` (HMLR zip basenames without `.zip`, e.g. `"Vale_of_Glamorgan_Council"`, every authority whose bbox intersects the survey bbox, sorted), `AUTHORITY_INDEX_PATH`, `load_authority_index() -> dict[str, tuple[float, float, float, float]]` (name to WGS84 west, south, east, north), `class InspireError(RuntimeError)`.

**Mechanics:** the committed `authority_index.json` maps each of the 318 HMLR names to a WGS84 bbox. The generator script (run by hand once, like `make_fixture.py` for OSTN15) scrapes the download page for the exact zip names (cookie session), then resolves each name to a bounding box via the ONS Geoportal's open local-authority-districts boundaries API (OGL; the script documents the exact endpoint and the name normalisation it applied: HMLR says "Cardiff_Council", ONS says "Cardiff"; strip `_Council`, `_Borough_Council`, `_County_Borough_Council`, `_District_Council`, `_City_Council` suffixes and match case-insensitively; every failed match is listed in the script's output and resolved by hand in the committed JSON, never silently dropped). Bboxes are padded by 1 km so an extent brushing a boundary pulls both neighbours, which is also what HMLR's own cross-file duplication expects. The four home councils' entries are hand-checked against known geography and asserted in tests by name.

- [ ] **Step 1: failing tests**: Llantwit-Major's bbox returns exactly `["Vale_of_Glamorgan_Council"]`; a bbox spanning the Cardiff/Vale border returns both; a Scottish bbox returns `[]` (INSPIRE covers England and Wales only); the index file has 318 entries and every name matches `^[A-Za-z_.]+$` (no spaces, no path separators: these become URL path segments and cache filenames).
- [ ] **Step 2: RED.** **Step 3: run the generator for real once, commit the JSON, implement the lookup.** **Step 4: GREEN.**
- [ ] **Step 5: Commit** (explicit paths).

---

### Task 2: download, cache, and the streaming parcel parser

**Files:**
- Modify: `src/mapgen/sources/inspire.py`
- Create: `tests/fixtures/inspire/parcels_sample.gml` + `tests/fixtures/inspire/make_gml_fixture.py`
- Test: `tests/test_inspire.py`

**Interfaces:**
- Produces: `fetch_authority_zip(name: str, session, cache_dir: Path | None = None) -> Path` (month-stamped cache `~/.mapgen/inspire/<name>_<YYYY-MM>.zip`; a cache hit never touches the network; the download goes through ONE `requests.Session` shared with the page fetch, because the service's cookie handshake requires it, and streams to the cache path atomically), `parcels_in(zip_path: Path, bbox_bng: tuple[float, float, float, float]) -> Iterator[list[list[tuple[float, float]]]]` (each parcel as rings, exterior first, coordinates in BNG metres; only parcels whose exterior-ring bbox intersects `bbox_bng`; `iterparse` over the zip member opened as a stream, `elem.clear()` per member), plus `ParseCounts` (parcels_seen, parcels_kept, parcels_skipped_malformed).

**Binding details:** namespaces exactly as probed (`LR="www.landregistry.gov.uk"`, wfs 2.0, gml 3.2); refuse an `srsName` that is not EPSG 27700 with a sentence; a `posList` with an odd token count or a non-numeric token counts the parcel malformed and skips it; interior rings are parsed and carried after the exterior. Stale cache months are swept (any `<name>_*.zip` older than the current month is deleted after a successful new download, never before).

- [ ] **Step 1: build the fixture**: `make_gml_fixture.py` extracts roughly 10 real parcels (including at least one pair sharing an edge and, if present in the source slice, one with an interior ring) from a real authority download into `parcels_sample.gml` (a valid, tiny `wfs:FeatureCollection`), committed at a few tens of KB. Failing tests: parse counts; bbox filtering in and out; malformed posList skipped and counted; srsName refusal; cookieless-session failure pinned (a fake that refuses cookies reproduces the 302 loop shape; a cookie-carrying fake succeeds); cache hit does no network (session that raises); month sweep.
- [ ] **Step 2: RED.** **Step 3: implement.** **Step 4: GREEN, plus one `live` test: fetch the real Vale of Glamorgan zip through the real handshake, parse parcels over a small Llantwit bbox, assert a plausible non-zero count; run once, record size and wall time in the report.**
- [ ] **Step 5: Commit.**

---

### Task 3: `boundary_curves.py`, rings to deduplicated curves

**Files:**
- Create: `src/mapgen/boundary_curves.py`
- Test: `tests/test_boundary_curves.py`

**Interfaces:**
- Produces: `boundary_curves(parcels: Iterable[list[list[tuple[float, float]]]]) -> list[list[tuple[float, float]]]` in BNG metres.

**Algorithm (binding):**
1. **Duplicate-ring removal**: the same parcel arrives from two authority files with identical geometry; hash each ring's rounded (0.01 m) coordinate sequence, normalised for direction (compare against its reversal, keep the lexicographically smaller) and for starting vertex (rotate a closed ring to start at its smallest vertex); drop exact duplicates.
2. **Edge extraction and shared-edge dedup**: every ring (exterior and interior) becomes its consecutive vertex-pair edges; an edge and its reversal are the same edge (normalise); edges appearing more than once (the party wall between two parcels, drawn in both) collapse to ONE copy. This is what turns parcel polygons into the single-line boundary drawing the owner asked for.
3. **Chaining**: unique edges join into maximal polylines through degree-2 vertices (same endpoint-hash approach as `contours.py`'s `_join_segments`; a vertex where 3+ edges meet is a junction and ends every chain touching it). Collinear vertices dropped at 0.05 m, same constant as contours.

- [ ] **Step 1: failing tests** on hand-built cases: two squares sharing one edge yield exactly 7 edges chained into the correct outline (the shared edge appears once); an identical duplicated square yields the square once; a T-junction ends chains at the junction vertex; direction and rotation normalisation (same ring reversed/rotated is one ring); an interior ring (courtyard) survives as its own closed curve.
- [ ] **Step 2: RED.** **Step 3: implement.** **Step 4: GREEN, including one test on the real fixture parcels asserting the shared-edge pair from Task 2's fixture collapses.**
- [ ] **Step 5: Commit.**

---

### Task 4: the LayerSource wiring and the boundaries GeoJSON

**Files:**
- Modify: `src/mapgen/sources/inspire.py`, `src/mapgen/package.py` (`register_default_sources`), `src/mapgen/naming.py` if `check_path_length` needs the new name
- Test: `tests/test_inspire.py`

**The source (mirroring `LidarWalesSource`'s whole-area conventions exactly):**

```python
class InspireSource:
    id = "inspire"
    display_name = "Property boundaries (INSPIRE)"
    licence = "Open Government Licence v3.0"
    attribution = (
        "This information is subject to Crown copyright and database rights "
        "[year] and is reproduced with the permission of HM Land Registry. "
        "The polygons (including the associated geometry, namely x, y "
        "co-ordinates) are subject to Crown copyright and database rights "
        "[year] Ordnance Survey AC0000851063."
    )
    requires_api_key = False
```

with `[year]` substituted at merge time from the downloaded collection's timeStamp year, and the conditions link carried as a separate `conditions_url` attribute the provenance writer includes.

- `estimate()`: no network; `len(authorities_for(bbox))` times provisional `BYTES_PER_AUTHORITY = 15_000_000` (Bridgend measured 13.1 MB, 2026-08-06, one sample; refit in Task 7) with a seconds model floored at one handshake plus one zip; zero authorities estimates zero bytes.
- `fetch()`: reset `tile_failures`; cancel checkpoint; `authorities_for(bbox)` empty records `FAILURE_NO_OUTPUT` against every tile with `"INSPIRE Index Polygons cover England and Wales only, and this extent is outside both."` and raises; per-authority download (cancel checkpoint between authorities) via the shared classifiers on transport/status failures; parse each zip through `parcels_in` against the padded BNG extent (project the bbox corners via `to_bng`, both corners min/maxed, `PAD_METRES` applied, exactly `lidar_wales._padded_bng_extent`'s arithmetic: reuse it by promoting it to `bng.py` or importing it; one home only); write the kept parcels to ONE work file `parcels.jsonl` (one JSON ring-list per line, BNG coords) plus a small `meta.json` (per-authority counts, timeStamp year); `tile_done`/`tile_skipped` with `tile_id="whole-area"`; skip-on-resume when both work files exist non-empty.
- `merge(parts, out_dir, stem)`: pick its work files by name; run `boundary_curves`; unproject vertices via `from_bng` (grid via `load_ostn15()` then `ensure_ostn15()`; if both fail, skip with stale-unlink, documented, like lidar's contours); write `<stem>_boundaries.geojson`: FeatureCollection of LineStrings, each feature's properties exactly `{"source": "HM Land Registry INSPIRE Index Polygons", "note": "The extent of the land contained in any registered title cannot be established from the INSPIRE Index Polygons.", "year": <int>}`; stale-unlink when inputs are missing.
- `possible_outputs(stem)`: `[f"{stem}_boundaries.geojson"]`.
- Register in `register_default_sources`; verify `check_path_length` (the new name is shorter than the contours candidate; state the arithmetic in the report).

- [ ] **Step 1: failing tests** (offline, fake sessions pinning URLs, fixture GML): estimate arithmetic incl. zero-authority; fetch happy path writes both work files; outside-England-and-Wales refusal kind and sentence; per-authority transport failure recorded with the right kind and retried semantics; skip-on-resume; merge produces the GeoJSON with exact properties and dedup applied; registry and repeat-skip.
- [ ] **Step 2: RED.** **Step 3: implement.** **Step 4: GREEN plus one `live` test: full fetch+merge over a small Llantwit extent, asserting a non-zero curve count and the year in properties.**
- [ ] **Step 5: Commit.**

---

### Task 5: curves into the `.osm`, and the survey.json block

**Files:**
- Modify: `src/mapgen/package.py`
- Test: `tests/test_package.py` additions

**Interfaces:** `_fuse_boundaries_step(root, stem, sink) -> dict`, record `{"written": int|None, "curves": int|None, "kept_existing": int|None, "error": str|None}` (four keys always, the one-shape discipline), survey.json key `"inspire_boundaries"`, events `boundaries_fusion_started/written/failed/skipped`. Gated on `<stem>.osm` AND `<stem>_boundaries.geojson` both existing; runs BEFORE `_run_bridge_step` in BOTH `run_survey` and `bridge_package`, immediately after `_fuse_heights_step`.

**Mechanics:** read the GeoJSON, write each LineString into the `.osm` as a way tagged `boundary=property`, `source=hm_land_registry`, `note=` the indicative sentence, with new `<node>` elements for its vertices. Ids are negative, descending from -1, node and way ids from the same counter (the OSM convention for synthetic, never-uploaded data; verify Urbano's OSM reader accepts negative ids by checking the decompiled reader notes in `docs/urbano/README.md` first, and say what you found in the report; if it does not, fall back to ids continuing above the file's maximum, documented). Idempotent: any existing way with `source=hm_land_registry` means a prior fusion; count them all as `kept_existing`, inject nothing, write nothing (the bridge re-run case). Atomic rewrite exactly as `heights.py` does it (same tail-handling caveat; read `_rewrite_osm` first).

- [ ] **Step 1: failing tests**: injection produces parseable XML with the right tags and negative ids resolving correctly; idempotence (second run written=0, kept_existing=curve count); ordering (event sequence has boundaries fusion after heights fusion, before bridge) in both run_survey and bridge_package; the four-keys-always record on success, failure, skip; provenance block carries both attribution statements with the year substituted and the conditions link.
- [ ] **Step 2: RED.** **Step 3: implement.** **Step 4: GREEN.** **Step 5: Commit.**

---

### Task 6: the two carried-in items from the owner's release feedback

**Files:**
- Modify: `src/mapgen/contours.py` (conditionally), `docs/urbano/README.md`
- Create: `.superpowers` report only for the epoch investigation
- Test: `tests/test_contours.py` if contours change

**6a, contour Z:** determine from the decompiled Urbano import machinery (start at `docs/urbano/README.md`'s notes on Import Geojson File; use ilspycmd against `Urbano.SiteAnalysis.gha` only if the notes do not answer it) whether GeoJSON `[lon, lat, z]` coordinates survive import with z intact. If YES: write each contour vertex as `[lon, lat, elevation]` (GeoJSON-legal), keep the `elevation` property, update the contour tests' coordinate assertions, and note it in README. If NO: change nothing, record the evidence (class and method names) in `docs/urbano/README.md` so the question is never reopened from memory.

**6b, the epoch shift:** investigation and report only, NO behaviour change. Quantify the expected offset between WGS84 (ITRF, current epoch, what OSM's GPS traces use) and ETRS89 (what OSTN15 and the LiDAR mosaics sit in) for south Wales in 2026, from the EUREF/ETRS89 plate-motion definition (about 2.5 cm/yr since 1989; state the derivation and the vector direction, not just the magnitude). Deliverable: a half-page report at `docs/superpowers/specs/2026-08-06-epoch-shift-note.md` stating the number, the direction, which mapgen outputs it affects (everything BNG-derived: LiDAR heights sampling, contours, boundaries), the proposed fix (one constant shift applied inside `to_bng`/`from_bng`, config-flagged), and what the owner should check visually before it is switched on. The owner decides; nothing ships in this task.

- [ ] **Steps: investigate 6a, implement or document; write 6b's note; run touched suites; commit** (explicit paths).

---

### Task 7: surface, refit, live proof, docs

**Files:**
- Modify: `README.md`, `docs/superpowers/HANDOFF.md`; `src/mapgen/sources/inspire.py` constants
- Test: extension of the live end-to-end test

- [ ] **Step 1: verify the UI checklist shows "Property boundaries (INSPIRE)" from the registry (expected diff: zero; record the reasoning).**
- [ ] **Step 2: refit `BYTES_PER_AUTHORITY` and the seconds model from Tasks 2/4's live measurements, measured-constant comment style.**
- [ ] **Step 3: live end-to-end over a Welsh extent with osm + lidar_wales + inspire: assert the `.osm` contains `boundary=property` ways, `<stem>_boundaries.geojson` exists with curves, survey.json's `inspire_boundaries` block and provenance (both statements, year, conditions link) are exact; record wall time and sizes.**
- [ ] **Step 4: README (package contents, survey.json schema, licences table row for HMLR INSPIRE with both statements; the indicative caveat sentence) and HANDOFF ("Then" points at item 3, OS Open + tier resolver).**
- [ ] **Step 5: Commit.**

---

## Decisions made by this plan

1. **Curves, not parcels.** Shared edges collapse to single lines and chains break at junctions: a boundary drawing, not a cadastral database. The parcels never appear as closed tagged areas anywhere.
2. **The committed authority index** (318 bboxes, 1 km padded) rather than a live boundary lookup: offline, testable, and the tier resolver in item 3 can replace it if it ever needs to.
3. **Month-stamped zip cache** in `~/.mapgen/inspire/`, swept after a successful newer download, matching HMLR's first-Sunday cadence.
4. **Fusion follows the heights pattern exactly**: a package.py post-step, four-keys-always record, idempotent by tag detection, before the bridge in both callers.
5. **Negative synthetic ids** pending the documented Urbano-reader check in Task 5.
6. **The epoch shift ships nothing without the owner's eyes on the number first.**

## Self-review notes

- Spec coverage: item 2's deliverable (tagged curves in the .osm, shared-edge dedup, both attributions per feature/provenance) is Tasks 3-5; the owner's two feedback items are Task 6; estimate honesty and docs are Task 7. Replacement machinery: still correctly absent.
- Type consistency: `parcels_in` yields ring lists consumed by `boundary_curves`; `authorities_for` names flow into `fetch_authority_zip`; the record shapes follow the established always-all-keys discipline.
- No placeholder step: where a value must come from the live service at implementation time (the ONS endpoint's exact URL, the Urbano Z answer), the task says which artifact settles it and what happens on each answer.
