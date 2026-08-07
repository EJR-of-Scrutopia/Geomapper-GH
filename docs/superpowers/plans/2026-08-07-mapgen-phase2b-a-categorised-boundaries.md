# Phase 2b Item A: Categorised Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every INSPIRE parcel classified by overlay against data the package already holds, and a `<stem>_boundaries_categorised.geojson` of boundary curves deduplicated WITHIN each category, so the owner types `category` = `garden` in Urbano's GeoJSON import and gets complete garden outlines.

**Architecture:** The inspire merge additionally persists closed parcel rings (`<stem>_parcels.geojson`); a new package step after the fusion steps classifies each parcel by sampled-majority overlay against the package's own layers (fused .osm buildings, Overture land_use/water, OS Open greenspace/land), groups parcels by category, runs the existing `boundary_curves` machinery per group, and writes the categorised file plus per-category counts into survey.json. Classification is sampled-majority, documented as `derived from map overlay, indicative` everywhere it appears.

**Tech Stack:** Python stdlib only; reuses `boundary_curves.py` (item 2) and the fusion-step conventions in `package.py`.

## Global Constraints

- No new third-party dependencies. No network in any package step.
- Nothing fabricated: a parcel no overlay covers is `unclassified`, never guessed.
- The HMLR attribution statements and the indicative caveat ride along exactly as in item 2; the new file's features carry `{"category", "source", "note", "year"}` with source/note/year identical to `_boundaries.geojson`'s.
- The spec (docs/superpowers/specs/2026-08-07-mapgen-phase2b-addendum-design.md, Item A) governs; the category vocabulary and priority rules below are copied from it.
- No em dashes anywhere. No AI attribution. Explicit-path `git add`. No URL in errors. merge()/step output filenames embed the stem.
- Suite baselines at plan time: Python 1760 passed / 16 deselected; Node 285 (`node tests/js/test_app.js`).
- Owner ground truth for the live proof: the Cowbridge package (13,204 parcels; the owner knows the town).

## Category vocabulary and rules (spec-exact, with the mapping table this plan pins)

Categories: `housing`, `garden`, `field`, `recreation`, `retail`,
`industrial`, `education`, `religious`, `allotments`, `water`,
`greenspace`, `woodland`, `unclassified`.

Priority (first match wins):
1. `housing`: >= 1 fused-.osm building footprint sample hit AND the parcel's land-use majority is residential.
2. From the Overture land_use majority, by this mapping: residential -> `garden` (building rule above already took `housing`), farmland/farmyard/meadow -> `field`, pitch/park/playground/recreation_ground/recreation -> `recreation`, retail -> `retail`, industrial/quarry -> `industrial`, school/education -> `education`, religious/cemetery -> `religious`, allotments -> `allotments`, grass -> `greenspace`; any other land_use class -> treated as no land-use evidence.
3. `water`: majority of samples inside Overture water polygons or closed .osm ways tagged natural=water.
4. `greenspace`: majority inside OS Open Greenspace site polygons (`_os_greenspace.geojson`, when present).
5. `woodland`: majority inside OS OpenMapLocal Woodland polygons (`_os_land.geojson` kind woodland, when present).
6. `unclassified`.

Majority means: of the parcel's interior sample points, the overlay class holding the most samples, ties broken by the priority order above. Sampling: a square grid over the parcel's bbox at spacing `clamp(sqrt(parcel_area_m2) / 8, 2.0, 20.0)` metres (converted to degrees with the local cos-latitude factor), keeping only points inside the parcel ring (ray cast); a parcel too small to catch 4 grid points gets its ring vertices' centroid-region samples instead (the `_representative_point` from buildings.py plus the 4 midpoints of its bbox edges clipped to inside, keeping whichever land: minimum 1 sample, the representative point, which is always interior). Sample count and spacing land in the record so the approximation is auditable.

---

### Task 1: persist the parcel rings

**Files:**
- Modify: `src/mapgen/sources/inspire.py` (merge writes `<stem>_parcels.geojson`; `possible_outputs` grows)
- Test: `tests/test_sources_os_open.py` is NOT touched; `tests/test_inspire.py` extends

**Interfaces:**
- Consumes: inspire's merge already iterates parcels (rings + year) from its work parts to build curves and the .osm fusion input.
- Produces: `<stem>_parcels.geojson`: FeatureCollection of closed Polygons (WGS84, [lon, lat], exterior ring only, first==last vertex), properties exactly `{"source": "HM Land Registry INSPIRE Index Polygons", "note": <the HMLR caveat verbatim from the module's existing constant>, "year": <int>}`. Feature order matches the work-part order (determinism). Zero parcels -> no file (matching the sibling outputs' zero-feature convention).

**Steps:**

- [ ] **Step 1: Failing tests.** Extend the existing inspire merge tests: the parcels file lands beside `_boundaries.geojson` with closed rings, exact properties, [lon, lat] order (assert the known fixture coordinate), zero-parcel run writes no file, `possible_outputs` includes the new name, stale-output sweep covers it.
- [ ] **Step 2: RED run** (`pytest tests/test_inspire.py -v`).
- [ ] **Step 3: Implement** inside the existing merge pass (one more writer alongside the curves writer; do not re-read work parts twice).
- [ ] **Step 4: Green; full Python suite.**
- [ ] **Step 5: Commit** `feat(boundaries): persist closed parcel rings in the package`.

### Task 2: the classifier core

**Files:**
- Create: `src/mapgen/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Consumes: `buildings.py`'s `_representative_point` and ray-cast helper (import them; if they are module-private, promote to module-level names without underscore in buildings.py as part of this task, keeping old names as aliases).
- Produces:
  - `@dataclass(frozen=True) class OverlaySets:` fields `buildings`, `landuse` (list of (class_name, ring) pairs), `water`, `greenspace`, `woodland` (each a list of rings, WGS84 exterior-only, holes ignored with a docstring note).
  - `classify_parcel(ring: list[tuple[float, float]], overlays: OverlaySets) -> tuple[str, int]`: (category, sample_count), implementing the vocabulary, priority, mapping table and sampling spec from the plan header EXACTLY.
  - `classify_parcels(rings: list, overlays: OverlaySets) -> list[tuple[str, int]]` with one shared spatial hash over the overlay rings (0.0005 degree cells, the buildings.py convention) so N parcels do not rebuild it N times.

**Steps:**

- [ ] **Step 1: Failing tests, synthetic geometry.** A parcel wholly inside a residential land_use ring with one building footprint -> housing; same without the building -> garden; farmland majority with a barn footprint -> field (the building rule requires residential majority: pin this exact case, it is the spec's own reasoning); a parcel straddling farmland (60% of samples) and residential (40%) -> field; water majority -> water; nothing -> unclassified; a sliver parcel too small for the grid -> classified from its representative point (1 sample, recorded); tie between two land-use classes -> the priority order breaks it; grass -> greenspace; cemetery -> religious.
- [ ] **Step 2: RED run.**
- [ ] **Step 3: Implement.** Grid sampling with cos-latitude spacing, ray cast per sample against the hashed overlay rings, majority + priority.
- [ ] **Step 4: Green. Mutation-check the priority order** (swap rules 1 and 2, at least one test must fail; machine traps: pyc mtimes, no multi-line \n regex on CRLF, no git checkout -- reverts).
- [ ] **Step 5: Commit** `feat(classify): sampled-majority parcel classifier`.

### Task 3: the package step and the categorised file

**Files:**
- Modify: `src/mapgen/package.py` (`_categorise_boundaries_step`, ordering after buildings fusion, survey.json record, bridge_package parity)
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: `<stem>_parcels.geojson` (Task 1), `classify.py` (Task 2), `boundary_curves.boundary_curves_with_counts` (item 2), the fused `.osm` (building ways incl. source=overture/os_openmap_local, natural=water closed ways), `<stem>_land_use.geojson` + `<stem>_water.geojson` (Overture, optional), `<stem>_os_greenspace.geojson` + `<stem>_os_land.geojson` (optional).
- Produces: `<stem>_boundaries_categorised.geojson`: for each category with >= 1 parcel, the deduped curves of THAT category's parcels (each feature `{"category", "source", "note", "year"}`); survey.json key `"boundaries_categories"` with keys ALWAYS `{"parcels", "counts": {<category>: n, ...}, "samples", "spacing_note", "error"}` where spacing_note is the literal string `derived from map overlay, indicative`; events `boundaries_categories_started/finished/failed`; runs in run_survey AND bridge_package after the buildings fusion step; missing parcels file -> counts all zero, error null (a package without inspire simply has no parcels); idempotent (re-run overwrites the file, byte-identical for identical inputs).

**Steps:**

- [ ] **Step 1: Read `_fuse_boundaries_step` and `_fuse_buildings_step` end to end;** mirror their record/event/parity conventions exactly.
- [ ] **Step 2: Failing tests.** Fixture package with parcels + overlays: the file lands, curves grouped per category, a shared garden/field edge appears once under EACH of the two categories (the whole point: pin it), record keys always present, no-inspire package clean, bridge parity, event order, byte-identical re-run.
- [ ] **Step 3: RED run.**
- [ ] **Step 4: Implement.** Overlay loading tolerates each optional file's absence; .osm water rings resolved via the existing node-ref helpers.
- [ ] **Step 5: Green; full Python suite.**
- [ ] **Step 6: Commit** `feat(boundaries): categorised boundary curves in the package`.

### Task 4: live proof and docs

**Files:**
- Modify: `tests/test_live_smoke.py`, `README.md`, `docs/urbano/README.md`, `docs/superpowers/HANDOFF.md`

**Steps:**

- [ ] **Step 1: Live test** (`@pytest.mark.live`): full run over the Cowbridge extent with osm+overture+inspire+os_open: the categorised file lands; assert `garden` and `field` are both non-empty and their sum is a majority of classified parcels (the owner knows this town: it is houses and farmland); assert `unclassified` is under half of all parcels; print the full per-category counts for the report and the owner.
- [ ] **Step 2: Docs.** README survey.json table row for `boundaries_categories`; docs/urbano/README.md documents the file and the owner's exact workflow (Import Geojson File, filter key `category`, value `garden` etc., with the note that a shared edge appears once per category so single-category imports are complete outlines); HANDOFF marks item A shipped, next item B (roofs + canopy) INCLUDING the spec's new resolution gate and validation requirements (quote their existence, not their text). No em dashes.
- [ ] **Step 3: Full suites, commit** `docs(boundaries): categorised curves proven over cowbridge`.

## Self-review notes

- Spec coverage: vocabulary, priority, sampled-majority, within-category dedup, indicative wording, per-category counts: all tasked. The spec's "OSM water tags" is narrowed to closed natural=water ways (stated in Task 3); raw .osm landuse tags are NOT consulted since Overture land_use already carries OSM's own landuse data for these extents (verified in the Cowbridge diagnosis of 2026-08-06: identical class distributions).
- Type consistency: rings as [lon, lat] float pair lists throughout; OverlaySets is the one container.
- No placeholders; the mapping table and sampling spec are pinned in the header.
