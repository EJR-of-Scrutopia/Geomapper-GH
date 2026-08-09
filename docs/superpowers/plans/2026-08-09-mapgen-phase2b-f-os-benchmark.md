# Phase 2b Item F: The OS Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `mapgen benchmark <package-dir>` command that measures a package's open-stack outputs (OSM + Overture + OS Open fusions) against OS's survey-grade NGD data over the same extent, producing a markdown + JSON report of per-category counts, building-footprint IoU distributions, and road-centreline offset statistics whose mean vector settles the epoch-shift question, all behind a binding derived-data firewall.

**Architecture:** Three new modules: `ngd.py` (an in-memory OGC API Features client for the OS NGD API, dev-mode key, paged, BNG-native), `benchstats.py` (pure sampled-IoU, greedy matching and point-to-polyline offset machinery, no network, no disk), `benchmark.py` (reads the package, pulls NGD over its bbox, computes, writes `benchmarks/<stem>_<date>/report.md` + `report.json`, discards every premium byte). Plus `mastermap_gml.py`, the reusable MasterMap Topography Layer reader the spec wants shared with the deferred premium-import path; parser only, wired into nothing. The owner is creating the dev-mode key now; Tasks 1-4 test synthetically, Task 5 runs the live benchmark over the real Cowbridge package.

**Tech Stack:** Python stdlib only. Reuses `buildings.point_in_ring`, `heights._percentile`, `bng.to_bng`/OSTN15, `xml.etree.iterparse` (the `os_gml.py` conventions), the suite's stub-session and live-marker patterns.

## Global Constraints

- **THE FIREWALL (binding, spec verbatim in substance, a review criterion
  for EVERY task):** nothing from any OS premium product enters a
  package, a fusion, a shard cache, or any output a client could
  receive. The benchmark reads premium data IN MEMORY, computes
  statistics, writes the report, and discards the data. No geometry, no
  feature identifier, no attribute value, and no correction derived from
  premium data flows into mapgen outputs. The report carries AGGREGATES
  ONLY: counts, distribution statistics, offset vectors, class-name
  frequency tables. `ngd.py` performs no disk write of any kind.
- **The key is radioactive:** supplied via `--key` or the `OS_NGD_KEY`
  environment variable ONLY. Never written to config.json, never to any
  report, never to a package, never echoed in an exception message or a
  log line (an error that quoted the failing URL would leak it: the
  standing no-URL-in-errors rule does double duty here and is
  load-bearing). Tests assert the key's absence from every artifact and
  every raised message.
- **Dev-mode honesty:** every report opens with a header naming the data
  source (OS NGD via a dev-mode evaluation key), the pull date, and that
  the numbers exist for internal calibration. `benchmarks/` is added to
  `.gitignore`: reports stay local; what gets committed is any
  tier-table or documentation change they motivate, citing numbers.
- Standing: stdlib only, no build step; no em dashes anywhere; no AI
  attribution; explicit-path `git add`; live tests behind the `live`
  marker, and the NGD live tests SKIP HONESTLY when `OS_NGD_KEY` is
  unset (the os_uprn skip precedent); token-gated routes untouched.
- Suites: `PYTHONPATH=src C:/Python313/python.exe -m pytest tests/ -m
  "not live" -q` (baseline 1932 passed / 18 deselected) and
  `node tests/js/test_app.js` (285). The miniforge base Python is broken
  on this machine (owner item): use C:/Python313 with PYTHONPATH=src for
  every Python invocation.

## Probed facts (2026-08-09, live; cite this table, do not re-derive)

- Endpoint root: `https://api.os.uk/features/ngd/ofa/v1`. OGC API
  Features. `/collections` and `/api` (OpenAPI) answer KEYLESS;
  `/collections/{id}/items` answers 401 keyless or with a bad key, body
  `{"code": 401,"description": "Missing or unsupported API key provided."}`.
- Auth (from the API's own OpenAPI document): apiKey named `key`, in
  QUERY or HEADER (both official). Use the HEADER form in the client:
  a key in a query string reappears in any echoed URL, and next links
  carry query strings.
- `/items` parameters: `limit` (min 1, MAX 100), `bbox`, `bbox-crs`,
  `crs`, `filter` (CQL). Paging by `links[rel=next]`.
- CRS: EPSG 27700 is requestable for both `crs` and `bbox-crs` (storage
  is 7405, BNG + ODN). The reference side of every comparison is
  BNG-native survey-grade: no reprojection on their side, `to_bng` on
  ours, which is exactly what makes the offset statistics an epoch
  answer.
- 94 collections. The benchmark uses `bld-fts-buildingpart-2` (physical
  building footprints) and `trn-ntwk-roadlink-5` (road centrelines).
  Version suffixes can bump: the client verifies its collection ids
  against the keyless listing at start and fails with a message naming
  the drift (no URL) if one is gone.
- Volume sanity: a Cowbridge-sized extent holds a few thousand building
  parts and about a thousand road links: under 60 pages at limit=100,
  trivial against the dev-mode allowance. `MAX_PAGES = 500` is the
  runaway guard.

## File Structure

- Create: `src/mapgen/ngd.py` (Task 1), `src/mapgen/benchstats.py`
  (Task 2), `src/mapgen/benchmark.py` (Task 3),
  `src/mapgen/mastermap_gml.py` (Task 4)
- Create: `tests/test_ngd.py`, `tests/test_benchstats.py`,
  `tests/test_benchmark.py`, `tests/test_mastermap_gml.py`
- Modify: `src/mapgen/cli.py` (Task 3: the `benchmark` subcommand),
  `.gitignore` (Task 3: `benchmarks/`)
- Modify: `README.md`, `docs/superpowers/HANDOFF.md` (Task 5)

---

### Task 1: The NGD client (`ngd.py`), in-memory only

**Files:**
- Create: `src/mapgen/ngd.py`
- Test: `tests/test_ngd.py`

**Interfaces:**
- Produces (Task 3 consumes):
  - `NgdError(RuntimeError)` with `kind` in `("auth", "listing",
    "query", "parse", "cap")` and optional `status_code`,
    `retry_after_seconds`. NO URL and NO KEY in any message, ever.
  - `NgdClient(key: str, session=None, timeout_seconds=30.0)` following
    the repo's session conventions (read how `os_downloads.py` builds
    requests):
    - `verify_collections(ids: Sequence[str]) -> None`: keyless
      `/collections` pull; raises `NgdError("listing")` naming any id
      absent from the listing ("collection X is not in the service's
      own listing; the version suffix may have moved").
    - `items(collection_id: str, bbox_bng: tuple[float, float, float,
      float], max_pages: int = MAX_PAGES) -> tuple[list[dict], int]`:
      every feature over the bbox as GeoJSON-shaped dicts IN MEMORY,
      plus the page count. `limit=100`, `crs` and `bbox-crs` both
      EPSG 27700 (the URIs from the probed-facts table), the key in the
      `key` HEADER (never the query string), following `links` with
      `rel == "next"` until absent. Exceeding `max_pages` raises
      `NgdError("cap")` with the page count in the message.
    - HTTP mapping: 401/403 -> "auth" ("the OS NGD key was refused; a
      dev-mode Premium project with the NGD Features API added is what
      answers here"); 429 -> "cap" with `retry_after_seconds` parsed
      from the Retry-After header when present; other non-200 ->
      "query" with the status code; unparseable JSON -> "parse".
  - `MAX_PAGES = 500`; `NGD_ROOT` constant; `BUILDING_COLLECTION =
    "bld-fts-buildingpart-2"`; `ROAD_COLLECTION = "trn-ntwk-roadlink-5"`.
- This module NEVER writes to disk. Its docstring says so and says why
  (the firewall).

- [ ] **Step 1: Failing tests** (`tests/test_ngd.py`), stub-session
  pattern from the existing suite: two-page paging follows the next
  link and returns 200 features + page count 2; the key travels in the
  header and never in any URL the stub sees; 401 -> NgdError kind
  "auth" whose message contains neither the key string nor "http";
  429 with Retry-After: 7 -> kind "cap", retry_after_seconds == 7.0;
  max_pages=1 against a two-page stub -> kind "cap"; garbage JSON ->
  "parse"; verify_collections against a stub listing missing one id ->
  "listing" naming it. Plus ONE live-marked test that SKIPS with a
  clear reason when OS_NGD_KEY is unset, and with it set pulls limit=1
  buildings over a 200 m Cowbridge bbox and asserts one feature with
  eastings in the 310000-320000 band (BNG arrived, not lon/lat).
- [ ] **Step 2: Watch them fail** (PYTHONPATH=src C:/Python313/python.exe -m pytest tests/test_ngd.py -v)
- [ ] **Step 3: Implement**
- [ ] **Step 4: Full offline suite green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/ngd.py tests/test_ngd.py`
  `git commit -m "feat(ngd): in-memory OGC features client for the OS benchmark"`

---

### Task 2: Comparison machinery (`benchstats.py`), pure and synthetic-tested

**Files:**
- Create: `src/mapgen/benchstats.py`
- Test: `tests/test_benchstats.py`

**Interfaces:**
- Produces (Task 3 consumes; rings are BNG (e, n) sequences, polylines
  are BNG (e, n) sequences):
  - `sampled_iou(ring_a, ring_b, step=0.5, sample_cap=20000) -> float`:
    grid over the two bboxes' union at `step` metres, each point tested
    with `buildings.point_in_ring` against both rings; IoU =
    in-both / in-either; 0.0 when in-either is 0. Over `sample_cap`
    the step widens (the classify.py SAMPLE_CAP precedent, cite it).
  - `match_footprints(ours: list[ring], theirs: list[ring]) ->
    MatchResult(matched: list[tuple[int, int, float]], unmatched_ours:
    list[int], unmatched_theirs: list[int])`: candidate pairs from a
    50 m cell index on bbox overlap, greedy best-IoU-first matching
    with a floor of `MATCH_IOU_FLOOR = 0.1` (below it a pair is not the
    same building), each feature matched at most once.
  - `polyline_offsets(ours: list[polyline], theirs: list[polyline],
    sample_every=5.0, search_radius=15.0) -> OffsetStats(count,
    mean_de, mean_dn, magnitude_of_mean, std_de, std_dn, p50_abs,
    p90_abs)`: densify OUR polylines every `sample_every` metres; for
    each sample find the nearest point on any of THEIR segments within
    `search_radius` (a 25 m cell index over their segments bounds the
    search); the offset VECTOR is (theirs_nearest - ours_sample);
    aggregate. Points with no neighbour inside the radius are counted
    `unmatched_samples`, never guessed.
  - `distribution(values, fractions=(0.1, 0.5, 0.9))` via
    `heights._percentile`.
- No network, no disk, no randomness anywhere in this module.

- [ ] **Step 1: Failing tests**, all synthetic with hand-checkable
  answers: identical 10 m squares -> IoU within 0.02 of 1.0; a square
  and its half-overlap -> within 0.02 of 1/3; disjoint -> 0.0; a
  concave L against its bbox rectangle -> the hand-computed ratio
  within 0.03. Matching: two of ours vs three of theirs with known
  overlaps -> the greedy pairing and the leftovers, exactly; a sliver
  overlap under the floor stays unmatched. Offsets: a 100 m straight
  line vs the same line shifted (+0.9, +0.9) -> mean vector within
  0.01 of (0.9, 0.9), std under 0.01, magnitude ~1.27; perpendicular
  grid streets shifted diagonally recover the same vector; a sample
  farther than the radius lands in unmatched_samples.
- [ ] **Step 2: Watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Full offline suite green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/benchstats.py tests/test_benchstats.py`
  `git commit -m "feat(benchstats): sampled IoU, greedy matching and offset vectors"`

---

### Task 3: `mapgen benchmark` and the report (`benchmark.py`)

**Files:**
- Create: `src/mapgen/benchmark.py`
- Modify: `src/mapgen/cli.py`, `.gitignore`
- Test: `tests/test_benchmark.py`

**Interfaces:**
- `run_benchmark(package_dir: Path, key: str, out_root: Path =
  Path("benchmarks")) -> tuple[Path, Path]` (the md and json paths)
- CLI: `mapgen benchmark <package-dir> [--key KEY] [--out DIR]`; the key
  resolves `--key` first, then `OS_NGD_KEY`, else a two-line refusal
  telling the user both routes (no URL).

Mechanics, pinned:

- Package reading: `survey.json` gives the stem and bbox; buildings are
  every `building=*` way in `<stem>.osm` (the FUSED file: OSM +
  Overture + OS Open injections all carry source tags, and the report
  splits counts by `source` tag where present); roads are every
  `highway=*` way (OSM-derived) PLUS, when `<stem>_os_roads.geojson`
  exists, its LineStrings as a SEPARATE population labelled os_open
  (BNG-native control group: its offsets against NGD measure
  generalisation, not epoch, and the report says so).
- Everything of ours projects to BNG through `to_bng`/OSTN15 (cache-only
  first, fetch fallback, the `_fuse_heights_step` pattern).
- NGD pulls: `verify_collections` first, then buildingparts and
  roadlinks over the bbox, in memory, page counts recorded.
- Statistics: counts per category (ours by source vs NGD); footprint
  matching (matched count, matched fraction of ours and of theirs, IoU
  p10/p50/p90, unmatched both ways); road offsets for the OSM
  population and the os_open population separately (the epoch section
  interprets ONLY the OSM one); NGD `description` attribute frequency
  table vs our building tag values (class NAMES and counts only).
- The epoch section, pinned interpretation: the hypothesis on record
  (docs/superpowers/specs/2026-08-06-epoch-shift-note.md) is a roughly
  0.9 m north-east systematic shift. The report states the measured
  mean vector, its std and sample count, and one of three verdicts:
  CONSISTENT with the hypothesis (magnitude 0.5-1.5 m, bearing within
  45 degrees of north-east, std under twice the magnitude), NOT
  DETECTED (magnitude under 0.3 m), or INCONCLUSIVE (anything else,
  with the numbers speaking). The os_open control's near-zero offset
  (or not) is printed beside it.
- Report files: `benchmarks/<stem>_<YYYY-MM-DD>/report.md` and
  `report.json`, atomic writes. The JSON schema is the md's numbers,
  nothing more: `{"source": "OS NGD (dev-mode evaluation key)",
  "pulled": iso-date, "package": stem, "bbox": [...], "pages":
  {"buildings": n, "roads": n}, "counts": {...}, "buildings":
  {"matched": n, "matched_fraction_ours": f, "matched_fraction_theirs":
  f, "iou": {"p10": f, "p50": f, "p90": f}, "unmatched_ours": n,
  "unmatched_theirs": n}, "roads": {"osm": OffsetStats-shaped,
  "os_open": OffsetStats-shaped}, "classes": {"ngd_only": {...},
  "counts": {...}}, "epoch_verdict": "..."}`. NO coordinates, NO
  feature ids, NO attribute values beyond class names, NO key.
- `.gitignore` gains `benchmarks/` with a comment naming the dev-mode
  terms as the reason.
- FIREWALL TESTS (the review criterion made executable): with a stub
  client returning synthetic "premium" features whose coordinates and
  ids are sentinel strings/numbers, (a) the entire out_root after a run
  contains exactly two files; (b) `report.json` read back as text
  contains no sentinel coordinate, no sentinel id, and not the key;
  (c) no write happens anywhere under the package dir (fs snapshot
  before/after); (d) an NgdError mid-run leaves no partial report.
  Plus: the CLI with no key prints the two-line refusal and exits
  non-zero without network (socket-refusing).

- [ ] **Step 1: Failing tests** per the mechanics (stub client fixtures;
  a tiny synthetic package fixture with survey.json + .osm the way
  test_package.py builds them)
- [ ] **Step 2: Watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Both suites green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/benchmark.py src/mapgen/cli.py .gitignore tests/test_benchmark.py`
  `git commit -m "feat(benchmark): mapgen benchmark command behind the derived-data firewall"`

---

### Task 4: MasterMap Topography GML reader (`mastermap_gml.py`), parser only

**Files:**
- Create: `src/mapgen/mastermap_gml.py`
- Test: `tests/test_mastermap_gml.py`

The spec's shared machinery: the reader the optional owner-purchased
extract comparison would use, written reusably because the deferred
premium IMPORT path will consume it later. It is wired into NOTHING in
this plan (no CLI, no package step); its module docstring records both
future consumers and repeats the firewall sentence.

**Interfaces:**
- `MasterMapError(RuntimeError)` (no URL in messages)
- `iter_topography_features(source) -> Iterator[OsFeature]` where
  `OsFeature` is `os_gml.py`'s own dataclass (import it): streaming
  `iterparse` over a file path or binary stream, yielding
  `TopographicArea` (Polygon, outer + inner rings, BNG),
  `TopographicLine` (LineString) and `TopographicPoint` (Point)
  members, with `feature_type` set to the member name and `properties`
  carrying `descriptiveGroup`, `descriptiveTerm`, `theme` and `toid`
  where present.
- Follows `os_gml.py`'s established contracts: whole-stream failure on
  malformed content (`MasterMapError`), root-element validation, empty
  posList refusal (nothing fabricated), namespace tolerance via
  localname matching (MasterMap GML is `osgb`-namespaced GML 2.1.2,
  older than OML's; the parser matches on localnames the way os_gml
  does, and the brief's fixture is the contract).
- Fixture: the task first tries to fetch OS's published MasterMap
  Topography Layer SAMPLE data (a free sample supply exists on the OS
  website; if a keyless direct download is found, commit a SMALL
  excerpt, under 100 KB, as `tests/fixtures/mastermap_sample.gml` with
  its source URL in a sibling README line; OS sample data is published
  for evaluation and a test fixture is exactly that). If no keyless
  sample is reachable, fall back to the plan's handwritten fixture: a
  minimal two-feature `osgb:FeatureCollection` (one TopographicArea
  with an inner ring, one TopographicLine) written into the test file,
  matching the documented member/geometry element names; flag in the
  report which route was taken.
- [ ] **Step 1: Failing tests**: the fixture parses to the expected
  features (ring counts, first/last coordinates, properties); a
  truncated stream raises MasterMapError; an empty posList raises; a
  wrong root raises naming the expectation.
- [ ] **Step 2: Watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Full offline suite green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/mastermap_gml.py tests/test_mastermap_gml.py` (plus the fixture files if fetched)
  `git commit -m "feat(mastermap): reusable Topography Layer GML reader, parser only"`

---

### Task 5: The live benchmark over the real Cowbridge package, and the epoch verdict

BLOCKS ON THE KEY: the owner is creating a dev-mode Premium project.
The task's first act is checking `OS_NGD_KEY`; if unset, return
NEEDS_CONTEXT immediately so the controller can ask the owner, rather
than building anything speculative.

**Files:**
- Modify: `README.md`, `docs/superpowers/HANDOFF.md`
- Not committed: the report under `benchmarks/` (gitignored)

- [ ] **Step 1:** Copy the owner's real Cowbridge package
  (C:\Users\Param\Surveys\Vale-of-Glamorgan\2026-08-06_Cowbridge-with-Llanblethian)
  to the scratchpad (originals read-only, standing rule) and run
  `mapgen benchmark` against the copy with the key from the
  environment. Record wall time and page counts (the dev-mode
  transaction sanity check: expect under 60 pages per collection).
- [ ] **Step 2:** Read the report critically before believing it:
  matched fractions plausible against the known populations (1,841
  fused buildings; NGD will hold more, it maps every shed);
  IoU p50 for matched buildings should sit well above 0.5 (OSM
  building tracing is decent); the OSM road offset count should be in
  the thousands. Any wild number gets investigated before the report
  is summarised anywhere.
- [ ] **Step 3:** The epoch verdict, delivered with its numbers: the
  measured OSM mean vector against the 0.9 m NE hypothesis, the
  os_open control beside it, and what the verdict means for the
  config-flag proposal in the epoch-shift note (the owner decides the
  flag; this task delivers the evidence).
- [ ] **Step 4:** Docs: README gains a short benchmark section (what
  the command does, the key's two supply routes, the firewall sentence,
  benchmarks/ is local-only); HANDOFF gains the item F shipped
  paragraph with the headline numbers and the epoch evidence, and
  points item E and the epoch flag at the owner with the measured
  basis. Both suites green.
- [ ] **Step 5: Commit**
  `git add README.md docs/superpowers/HANDOFF.md`
  `git commit -m "docs(benchmark): item F shipped, epoch evidence delivered"`

---

## Self-Review

- **Spec coverage:** dev-mode NGD pulls over the owner's extents (Tasks
  1, 5); `mapgen benchmark` producing md + JSON with per-category
  counts, IoU distributions, centreline offset statistics answering the
  epoch question, and OS-only class listing (Tasks 2, 3, 5); output in
  a benchmarks/ directory never a package file (Task 3); the firewall
  as a binding review criterion with executable tests (Task 3, and the
  in-memory-only client in Task 1); the MasterMap GML parser as shared
  machinery, import deferred (Task 4). No gaps found.
- **Placeholder scan:** clean; every task pins its mechanics, constants
  and test constructions; Task 4's fixture has a defined fallback
  rather than a TBD.
- **Type consistency:** `NgdClient.items` returns (features, pages) and
  Task 3's report schema records pages; `OffsetStats` fields named
  identically in Task 2's interface and Task 3's JSON schema;
  `OsFeature` is imported from os_gml, not redefined. Checked.
