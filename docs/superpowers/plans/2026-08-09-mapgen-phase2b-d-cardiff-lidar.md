# Phase 2b Item D: Cardiff 25 cm LiDAR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new opt-in source `lidar_cardiff` that packages the NRW historic archive's ten 25 cm quarter-tiles (flown 23 March 2011, Creigiau and Pentyrch, north-west Cardiff) as `<stem>_lidar25_dsm.tif` and `<stem>_lidar25_dtm.tif`, with vintage and location honesty on every surface.

**Architecture:** A stdlib ESRI-ASCII-grid parser (`asc_grid.py`) turns the archive zips' `.asc` members (2000x2000 cells, millimetre units) into `BngWindow`s; the source (`sources/lidar_cardiff.py`) knows the ten tiles' envelopes as committed constants (probed live 2026-08-08, evidence in the plan workspace's probe-report.md), downloads and caches the two static zips once, and at merge time assembles the extent's intersection with the coverage into 0.25 m rasters written by the existing `geotiff_write` writer, refusing (with a recorded, UI-visible reason) extents whose intersection exceeds the raster pixel budget. Owner decision 2026-08-08: spec as written, the ten tiles only, no general archive source.

**Tech Stack:** Python stdlib only. Reuses `cog.BngWindow`, `geotiff_write.write_bng_geotiff`, `fsutil` atomic writes, the sources' own `LayerSource` protocol (`covers`/`tier`/`detail`/`estimate`/`fetch`/`merge`), `zipfile` (NOT `os_downloads.ZipReader`, which refuses these 2011-era zips; probe-verified).

## Global Constraints

Carried from the phase 2 spec and the addendum, plus item D's own:

- **Vintage honesty (spec, verbatim in substance):** every UI and
  provenance surface carries `flown 2011`. Heights fusion, roof forms and
  canopy stay on the 2020-2023 1 m data: nothing from the 2011 rasters
  feeds them, the `.egrid`, or the contours.
- **Location honesty (controller ruling, recorded here the way item C's
  model-aware ruling was):** the spec's display name said "central
  Cardiff"; the plan-time probe disproved that (the tiles are the
  Creigiau and Pentyrch corner of north-west Cardiff, ~2.5 km2). The
  spec's own purpose, honest copy, governs over its literal string. The
  display name is
  `LiDAR terrain (Creigiau and Pentyrch, north-west Cardiff, 25 cm, flown 2011)`
  and no copy anywhere says "central Cardiff".
- **25 cm claims:** this source's own surfaces may claim 25 cm (it is
  true here); no other source's copy changes. README's old "no copy
  anywhere claims 25 cm" sentence is updated honestly in Task 6.
- **The pixel budget is refused, never silently degraded:** at 0.25 m,
  `cog.MAX_WINDOW_PIXELS` (16,777,216) is ~1.02 km2. An extent whose
  coverage intersection exceeds it gets NO 25 cm rasters and a recorded
  reason (exact string pinned in Task 4); `detail()` says which outcome
  an extent will get BEFORE download. No overview fallback exists in
  this source: the archive published one resolution and mapgen either
  delivers it or says why not.
- **Licence:** Open Government Licence for Public Sector Information
  (OGL). Attribution verbatim, probed from the layer page:
  `Contains Natural Resources Wales information © Natural Resources Wales and Database Right. All rights Reserved.`
- Opt-in source, default off (the `os_uprn` pattern). `estimate()`,
  `covers()`, `tier()`, `detail()` never touch the network. No URL in
  any exception message or user-facing reason. Token-gated routes
  untouched. Atomic writes. No new dependencies, no build step.
- No em dashes anywhere. No AI attribution. Explicit-path `git add`.
  Live-network tests carry the `live` marker. Suites:
  `python -m pytest tests/ -m "not live" -q` (baseline 1841 passed / 17
  deselected) and `node tests/js/test_app.js` (285).

## Probed facts (2026-08-08, all live; do not re-derive, cite this table)

- WFS layer `geonode:nrw_lidar_tile_catalogue_archive`; the ten records
  are `resolution=0.25 AND _10k='ST17 '`.
- Zips (alive, no key, HEAD gives size, ranged GET gives 206):
  - `https://lle.blob.core.windows.net/lidar/25cm_res_ST17_2011_dsm.zip`, 45,011,591 bytes
  - `https://lle.blob.core.windows.net/lidar/25cm_res_ST17_2011_dtm.zip`, 38,784,302 bytes
- Each zip holds ten `.asc` members (`dsm_D01396XX_20110323_...` /
  `dtm_F01396XX_...`), one per quarter-tile, 2000x2000 cells, cellsize
  0.25, `xllcorner`/`yllcorner` integer metres, CRLF, values in
  MILLIMETRES (`mm_units` in every member name), `NODATA_value` header
  present. Members are self-describing: placement comes from each
  member's own header, never from its name.
- The ten envelopes (BNG metres, (e_min, n_min, e_max, n_max)):
  ST1076NE (310500, 176500, 311000, 177000); ST1077SE (310500, 177000,
  311000, 177500); ST1077NE (310500, 177500, 311000, 178000); ST1176NW
  (311000, 176500, 311500, 177000); ST1177SW (311000, 177000, 311500,
  177500); ST1177NW (311000, 177500, 311500, 178000); ST1176NE (311500,
  176500, 312000, 177000); ST1177SE (311500, 177000, 312000, 177500);
  ST1177NE (311500, 177500, 312000, 178000); ST1277SW (312000, 177000,
  312500, 177500).
- `os_downloads.ZipReader` refuses these zips ("central directory is
  corrupt: entry N does not start with the expected signature");
  stdlib `zipfile` reads them cleanly. Use `zipfile`.

## File Structure

- Create: `src/mapgen/asc_grid.py` (Task 1), `src/mapgen/sources/lidar_cardiff.py` (Tasks 2-4)
- Create: `tests/test_asc_grid.py`, `tests/test_lidar_cardiff.py`
- Modify: `src/mapgen/package.py` (Task 5: registration, provenance/licences)
- Modify: `tests/test_package.py`, `tests/test_api.py` if the sources list is asserted there (Task 5)
- Modify: `README.md`, `docs/urbano/README.md`, `docs/superpowers/HANDOFF.md` (Task 6)

---

### Task 1: ESRI ASCII grid parser (`asc_grid.py`)

**Files:**
- Create: `src/mapgen/asc_grid.py`
- Test: `tests/test_asc_grid.py`

**Interfaces:**
- Produces (Task 4 consumes):
  - `AscGridError(RuntimeError)` (no URL ever in its message)
  - `parse_asc(text: str, value_scale: float = 1.0) -> BngWindow`
  - `parse_asc_header(text: str) -> dict` (keys `ncols`, `nrows`,
    `xllcorner`, `yllcorner`, `cellsize`, `nodata_value`; reads ONLY the
    header lines, for cheap member-bounds checks before full parses)

Mechanics, pinned:

- Header: case-insensitive keys, whitespace-separated, in any order,
  ending at the first line whose first token parses as a number.
  `ncols`/`nrows` positive ints; `xllcorner`/`yllcorner`/`cellsize`
  floats; `nodata_value` optional (default -9999.0 per the ESRI
  convention). Malformed or missing required keys raise `AscGridError`
  naming the key, never the source URL.
- Values: `nrows` rows of `ncols` floats, first row is the NORTH edge
  (the ESRI convention), tolerant of CRLF and of scientific notation.
  A short row, a short file, or a non-numeric token raises
  `AscGridError` with row number. Every value equal to `nodata_value`
  becomes NaN; every other value is multiplied by `value_scale`
  (0.001 turns the archive's millimetres into metres).
- Result: `BngWindow(e_origin=xllcorner, n_top=yllcorner + nrows *
  cellsize, pixel_size=cellsize, width=ncols, height=nrows,
  values=array("f", ...))`, row-major north-first, exactly what
  `write_bng_geotiff` and `sample_bng` already speak.

- [ ] **Step 1: Write the failing tests** (`tests/test_asc_grid.py`):
  a 3x2 grid with one nodata value parsing to the right window shape,
  bounds and NaN placement; mm-units scaling (`value_scale=0.001`,
  85321 -> 85.321); CRLF and LF equally; header key case and order
  insensitivity; missing `ncols` raising `AscGridError` naming it; a
  short row raising with the row number; scientific notation
  (`1.5e2` -> 150.0); `parse_asc_header` returning the six keys without
  touching the value rows (hand it a file whose values are garbage and
  expect no error).
- [ ] **Step 2: Run and watch them fail** (`python -m pytest tests/test_asc_grid.py -v`)
- [ ] **Step 3: Implement** (~90 lines; float row parse via
  `line.split()`, `array("f")` filled row by row)
- [ ] **Step 4: Tests pass; full suite green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/asc_grid.py tests/test_asc_grid.py`
  `git commit -m "feat(asc): ESRI ASCII grid parser for the NRW archive members"`

---

### Task 2: The source's disk-and-arithmetic half (`lidar_cardiff.py`)

**Files:**
- Create: `src/mapgen/sources/lidar_cardiff.py`
- Test: `tests/test_lidar_cardiff.py`

Read `src/mapgen/sources/lidar_wales.py` and `os_uprn.py` first: this
source follows their shapes (BBox padding, Estimate construction,
routing_note, cache_root conventions) and this brief pins only what is
new. No network anywhere in this task's code paths.

**Interfaces:**
- Produces:
  - `LidarCardiffSource` with `id = "lidar_cardiff"`, `display_name =
    "LiDAR terrain (Creigiau and Pentyrch, north-west Cardiff, 25 cm,
    flown 2011)"`, `_TIERS = {"terrain": 0}` (above lidar_wales's 1;
    the resolver sorts ascending)
  - `COVERAGE_TILES`: the ten envelopes from the probed-facts table,
    verbatim, as a tuple of 4-tuples with the tile code in a comment
    beside each
  - `DSM_ZIP_URL`, `DTM_ZIP_URL`, `DSM_ZIP_BYTES = 45011591`,
    `DTM_ZIP_BYTES = 38784302`, `PIXEL_METRES = 0.25`,
    `FLOWN = "flown 23 March 2011"` (the date is probed, not assumed)
  - `covers(bbox) -> str`: "full" when every part of the padded extent
    lies inside the union of `COVERAGE_TILES`, "partial" when it
    intersects at least one tile, "none" otherwise. The union test
    works on the 500 m lattice: split the padded extent's bounding
    rectangle into the 500 m cells it touches and require every touched
    cell to be one of the ten (the tiles ARE 500 m lattice cells, so
    this is exact, not approximate).
  - `detail(bbox) -> str | None`, computed, never guessed:
    - covered ("full") and within budget: `25 cm at this extent, flown 2011`
    - covered ("full") but over budget: `25 cm needs an extent under
      about 1 x 1 km here (flown 2011)`
    - "partial": `25 cm over part of this extent, flown 2011` (with the
      over-budget sentence appended when the INTERSECTION is over
      budget)
    - "none": None
    Budget arithmetic shared with Task 4 as one helper
    `_window_pixels(bbox) -> int`: the pixel count of the padded
    extent's intersection with the coverage envelope at 0.25 m, the
    same number the merge will refuse on, so the preview and the
    refusal can never disagree.
  - `estimate(bbox, tiles) -> Estimate`: cold cache (either zip missing
    from the cache dir) charges `DSM_ZIP_BYTES + DTM_ZIP_BYTES` once
    and sets the os_uprn-style routing_note naming the one-time
    download; warm cache charges 0 network bytes and the seconds floor.
    `BYTES_PER_SECOND_ESTIMATE = 2_000_000` with a comment that Task 6's
    live run corrects it against measurement (the item 3 pattern).
  - `cache_dir() -> Path`: `~/.mapgen/lidar_cardiff` (the os shards'
    cache_root convention).

- [ ] **Step 1: Write the failing tests**: covers() full/partial/none
  (an extent wholly inside ST1177SW; one straddling the block's west
  edge; one in Barry), including the concave corner (an extent touching
  310500-311000 x 176000-176500, the missing SW cell UNDER ST1076NE,
  must be "partial" or "none" by the lattice test, never "full");
  detail() strings verbatim for all four outcomes (an over-budget case:
  the whole 2 x 1.5 km block = 40M pixels > 16,777,216); tier
  ordering (0, above lidar_wales's 1); estimate cold vs warm via a
  monkeypatched cache_dir; zero network proven by a socket-refusing
  fixture if the suite has one (search tests/ for the existing
  pattern; test_lidar_wales.py has the shape to copy).
- [ ] **Step 2: Run and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Full suite green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/sources/lidar_cardiff.py tests/test_lidar_cardiff.py`
  `git commit -m "feat(lidar_cardiff): coverage, tiers, detail and estimate for the ten 2011 tiles"`

---

### Task 3: fetch(): the two static zips, cached once, verified

**Files:**
- Modify: `src/mapgen/sources/lidar_cardiff.py`
- Test: `tests/test_lidar_cardiff.py`

Mechanics, pinned:

- `fetch()` follows the source protocol's shape (read how lidar_wales
  and os_uprn stage parts): it ensures both zips exist in
  `cache_dir()`, downloading whichever is missing with the repo's
  session conventions (timeout, User-Agent, atomic
  write-to-temp-then-rename via fsutil), size-verified against
  `DSM_ZIP_BYTES`/`DTM_ZIP_BYTES` (a mismatch deletes the temp and
  raises the source's own error type with kind "download", no URL in
  the message), then validated by opening with `zipfile.ZipFile` and
  checking `namelist()` is non-empty (a truncated blob fails here, not
  in merge). The 2011 data is static: no version sweep, no re-download
  when present and the right size.
- The parts handed to merge are the two cached zip paths.
- A NOTE in the module docstring, verbatim in substance:
  `os_downloads.ZipReader refuses these 2011-era zips (central
  directory signature mismatch, probed 2026-08-08); stdlib zipfile
  reads them, and at 45 MB whole-download is the right shape anyway.`

- [ ] **Step 1: Failing tests**: cached-and-right-size means no
  download attempted (socket-refusing fixture); wrong-size cache entry
  is re-downloaded; a size-mismatched download raises kind "download"
  with no URL in the message; a non-zip payload fails validation. Plus
  ONE live-marked test (`@pytest.mark.live`) that downloads the real
  DSM zip headers... no: live tests are expensive but this one is the
  item's whole risk. The live test downloads BOTH real zips into the
  real cache (83.8 MB once; subsequent runs find them cached), asserts
  sizes match the two constants exactly and `zipfile` lists ten members
  each.
- [ ] **Step 2: Run and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Full suite green (live deselected); run the live test
  once (`python -m pytest tests/test_lidar_cardiff.py -m live -v`) and
  record its wall time in the task report**
- [ ] **Step 5: Commit**
  `git add src/mapgen/sources/lidar_cardiff.py tests/test_lidar_cardiff.py`
  `git commit -m "feat(lidar_cardiff): fetch and verify the two archive zips once"`

---

### Task 4: merge(): members to rasters, budget refused honestly

**Files:**
- Modify: `src/mapgen/sources/lidar_cardiff.py`
- Test: `tests/test_lidar_cardiff.py`

Mechanics, pinned:

- `merge(parts, out_dir, stem)` writes `<stem>_lidar25_dsm.tif` and
  `<stem>_lidar25_dtm.tif` (spec-pinned names) and returns their paths.
- Budget gate FIRST, before any member is parsed: if
  `_window_pixels(bbox) > cog.MAX_WINDOW_PIXELS`, raise the source's
  error with this exact reason (it reaches the UI through the ordinary
  source-failure event, and detail() already warned):
  `this extent needs {pixels:,} pixels at 25 cm and the raster budget
  is 16,777,216; extents under about 1 x 1 km inside the covered block
  come back at 25 cm`
- Window: the padded extent's intersection with the coverage envelope,
  snapped OUTWARD to the 0.25 m pixel lattice anchored at integer
  metres (xllcorner values are integers, so pixel edges sit at
  multiples of 0.25 exactly). Fill with NaN, then for each zip member:
  `parse_asc_header` first; skip members whose bounds miss the window;
  full `parse_asc(text, value_scale=0.001)` for the rest; paste the
  overlapping pixels (integer offsets both sides; the shared lattice
  makes them exact, assert the arithmetic with a comment, no
  resampling anywhere).
- A member that fails to parse fails the merge (one flight, one
  product, partial delivery would be a raster with silent holes where
  data exists; the owner's nothing-fabricated rule cuts both ways: the
  package either holds what the archive published over the extent or
  says why not).
- Pixels of the window outside the ten tiles stay NaN and read back as
  nodata: "partial" coverage yields an honest raster with the
  uncovered area absent, matching how the Welsh mosaic already answers
  nodata over England.
- Write via `write_bng_geotiff`, atomic by that writer's own contract.

- [ ] **Step 1: Failing tests**, using synthetic two-member zips built
  by the test (small ncols/nrows, mm-units values, real headers on the
  500 m lattice): a window spanning two members pastes both with exact
  seams (assert specific pixel values through `sample_bng`); mm became
  metres; a member wholly outside the extent is never fully parsed
  (monkeypatch parse_asc to count calls); the budget refusal message
  verbatim with the pixel count substituted (assert the number, the
  item B lesson: pin the substitution, not just the prose); the
  over-budget path parses NO member; a partial-coverage window has NaN
  exactly where no tile is; a corrupt member fails the whole merge.
- [ ] **Step 2: Run and watch them fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Full suite green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/sources/lidar_cardiff.py tests/test_lidar_cardiff.py`
  `git commit -m "feat(lidar_cardiff): merge archive members into 25 cm rasters"`

---

### Task 5: Registration, provenance, opt-in UI row

**Files:**
- Modify: `src/mapgen/package.py`
- Test: `tests/test_package.py` (and `tests/test_api.py` if it asserts
  the sources list; grep before assuming)

Mechanics, pinned:

- Register `LidarCardiffSource` beside the other sources (read how
  os_uprn registers and surface the same opt-in default-off behaviour;
  no configure() seam is needed, nothing here is configurable).
- survey.json provenance/licences for packages that selected it:
  licence `Open Government Licence for Public Sector Information (OGL)`
  and the attribution VERBATIM from the Global Constraints (copyright
  sign included), plus a vintage clause in the source's provenance
  entry: `flown 23 March 2011; buildings and ground changed since`.
- The resolver needs no code change (covers/tier/detail are source
  methods), but the estimate payload and survey.json must show
  lidar_cardiff at tier 0 for terrain with lidar_wales beside it where
  both are selected and covered: one test proves the resolver output
  shape over a Creigiau bbox with both selected (lidar_cardiff base,
  lidar_wales backup) and one over Barry (lidar_cardiff absent or
  coverage "none": read resolver.py's rule for none-coverage and assert
  what it actually does).
- Deliberate NON-wiring, asserted by tests: `_fuse_heights_step`,
  `_fit_roofs_step`, `_canopy_step` and the `.egrid`/contours paths key
  on `<stem>_lidar_dtm.tif`/`_lidar_dsm.tif` and MUST ignore
  `_lidar25_*` files entirely (a fixture package holding ONLY the 25 cm
  pair sees heights/roofs/canopy skip exactly as if no lidar were
  present). This is the spec's exclusion made structural.

- [ ] **Step 1: Failing tests** per the mechanics above
- [ ] **Step 2: Run and watch them fail**
- [ ] **Step 3: Implement (registration + provenance only; the steps
  already key on the other filenames, the tests PROVE it rather than
  change it)**
- [ ] **Step 4: Both suites green**
- [ ] **Step 5: Commit**
  `git add src/mapgen/package.py tests/test_package.py` (plus
  test_api.py if touched)
  `git commit -m "feat(package): lidar_cardiff registered, opt-in, provenance recorded"`

---

### Task 6: Live proof over the real block, measured constants, docs

**Files:**
- Modify: `src/mapgen/sources/lidar_cardiff.py` (only if the live run
  corrects `BYTES_PER_SECOND_ESTIMATE`)
- Modify: `README.md`, `docs/urbano/README.md`, `docs/superpowers/HANDOFF.md`

Mechanics:

- Live run: a real survey over a sub-budget extent inside the block
  (e.g. 311000-311900 x 176900-177800, 900 x 900 m = 12.96M pixels,
  under the budget) with `lidar_cardiff` selected; verify both tifs
  exist, open with `CogReader`, `pixel_size == 0.25`, plausible heights
  (Creigiau is ~90-130 m OD), NaN outside coverage if the extent
  clips; wall time and effective bytes/second recorded; correct
  `BYTES_PER_SECOND_ESTIMATE` if measurement disagrees materially (the
  item 3 pattern: the constant must not contradict this plan's own live
  run). Confirm the GH script reads the DSM
  (`python docs/grasshopper/lidar_to_mesh.py <dsm path>` CLI self-test
  mode) so the owner's Grasshopper route works at 0.25 m.
- Docs:
  - README: package-contents rows for the two files; a short source
    paragraph carrying location honesty (Creigiau and Pentyrch, ~2.5
    km2, the only 25 cm the archive holds over Cardiff), vintage
    honesty (flown 23 March 2011, fifteen years of change since), the
    budget rule (extents under about 1 x 1 km), and the exclusions
    (heights, roofs, canopy, terrain grid and contours all stay on the
    2020-2023 1 m data). UPDATE the roadmap's old "no copy anywhere in
    this project claims 25 cm" sentence honestly: the claim was written
    before this source existed; it becomes "the 25 cm claim exists only
    on the lidar_cardiff source's own surfaces, where it is true".
  - docs/urbano/README.md: how to pull the block (draw inside it,
    tick the source), what arrives, the GH mesh route at 0.25 m, and
    that the 2011 date makes it a historic surface, not a current one.
  - HANDOFF: item D shipped paragraph; next is item F per the build
    order; the owner's 2026-08-08 decision (spec as written, ten tiles,
    general archive source explicitly declined) recorded so no future
    session re-litigates it from the probe report alone.
- [ ] **Step 1: Run the live survey; record numbers**
- [ ] **Step 2: Correct the estimate constant if measurement says so**
- [ ] **Step 3: Write the docs; re-read each changed section against
  the shipped code (file names, strings, the budget number)**
- [ ] **Step 4: Both suites green**
- [ ] **Step 5: Commit**
  `git add README.md docs/urbano/README.md docs/superpowers/HANDOFF.md src/mapgen/sources/lidar_cardiff.py`
  `git commit -m "feat(lidar_cardiff): live-proven over the real block, documented"`

---

## Self-Review

- **Spec coverage:** new opt-in source, spec file names, same
  writer/reader/GH compatibility (Tasks 1, 4, 6); covers() from the ten
  tiles' REAL probed footprint (Task 2); tier above lidar_wales where
  covered (Tasks 2, 5); detail string with flown-2011 vintage (Task 2);
  vintage honesty on every surface (Tasks 2, 5, 6); heights/roofs stay
  on 2020-2023 data (Task 5's non-wiring tests); plan-time live probe
  requirement (done 2026-08-08, probe-report.md). Deviations from spec,
  both owner-visible: display name location honesty (ruling recorded in
  Global Constraints); detail() string extended for the budget and
  partial cases the spec never contemplated (its "25 cm at this extent,
  flown 2011" survives verbatim in the covered-and-fits case).
- **Placeholder scan:** clean. Tasks 1-4 pin exact algorithms, strings
  and numbers; Tasks 2-5 direct mirroring of named existing files for
  established scaffolding, with every novel value pinned here.
- **Type consistency:** `parse_asc -> BngWindow` is what
  `write_bng_geotiff` takes and Task 4 uses; `_window_pixels` is shared
  by detail() and merge() so the preview and the refusal cannot drift;
  `COVERAGE_TILES` 4-tuples are consumed by covers(), _window_pixels
  and the tests alike.
