# Phase 2 Item 3: OS Open Pack + Tier Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Two new sources built on the OS Data Hub Downloads API (OS Open Roads + OS OpenMap Local + OS Open Greenspace as `os_open`; OS Open UPRN as `os_uprn`), a buildings-category fusion step that fixes the owner's missing-buildings problem by injecting Overture and OS footprints the OSM base lacks, and the per-extent tier resolver from the spec: `covers()`/`tier()` on every source, a resolution record in the estimate and `survey.json`, and the tier list rendered in the web UI before Download.

**Architecture:** All OS products download keyless from `https://api.os.uk/downloads/v1` (entries redirect to Azure blob storage, which honours Range requests with HTTP 206, verified live 2026-08-07). Tiled products (OpenMapLocal, OpenGreenspace) download per 100km grid square; OpenRoads is a national zip whose members are per-square GML files, so a ranged zip reader pulls only the needed members (~40 MB per square instead of 608 MB); OpenUPRN is one national CSV member, streamed once and sharded. Every product is parsed once per version into gzipped NDJSON shards per grid cell under `~/.mapgen/osopen/`, and every later survey reads only its cells. GML parsing reuses the INSPIRE iterparse discipline; ranged reads reuse `cog.py`'s ByteSource machinery; BNG to WGS84 goes through `bng.py` only.

**Tech Stack:** Python stdlib only (urllib, zipfile, zlib, gzip, csv, json, xml.etree.iterparse, sqlite-free). No new dependencies, no build step.

**Scope note for the owner:** Boundary-Line (admin boundaries) is DEFERRED from the spec's product list: no consumer exists in the owner's workflow, and it joins later through this same client in a short task if wanted. The spec's other four products are all here.

## Global Constraints

Copied from the phase 2 spec and the project's standing rules. Every task's requirements include these.

- No new third-party dependencies. No build step. Python stdlib only.
- API keys must never be logged, echoed, or embedded. No URL ever enters an exception message, TileFailure.reason, or survey.json field that leaves a module. (OS Open needs no key; the rule still binds error text.)
- Every `/api/` route stays token gated. The page contacts only this machine and the tile servers.
- "If a tile has no data then it has no data": fusion adds what is missing, never overwrites, never fabricates.
- Fusion only, no replacement machinery (spec: replacement DEFERRED, no consumer).
- The 1 m resolution promise stands: no copy anywhere claims 25 cm.
- Attribution: OS Open products are OGL v3; the attribution statement is "Contains OS data © Crown copyright and database right [year]" with the year substituted from the product version at merge time, recorded in survey.json. Never paraphrase.
- Live-network tests carry the `live` marker (deselected by default via pyproject addopts).
- No em dashes anywhere: code, comments, docs, commit messages, UI copy.
- No AI attribution anywhere. The owner owns the work.
- Explicit-path `git add`, never `-A`.
- Injected OSM ids start strictly below the file's existing minimum id (use `package.py`'s `_minimum_existing_id`), and every injected way carries a `source` tag so re-runs are idempotent.
- Coordinate transforms go through `bng.py` (`from_bng`, `to_bng`, OSTN15) only; no second implementation.
- `estimate()` never touches the network (registry convention; INSPIRE precedent).
- merge() output filenames embed the stem (LayerSource.merge docstring rule).

## Verified facts every task may rely on (probe of 2026-08-07)

- Listing: `GET https://api.os.uk/downloads/v1/products/{id}/downloads` returns entries `{"md5", "size", "url", "format", "subformat"?, "area", "fileName"}`. No key. The `url` field 302-redirects to `omseprd1stdstordownload.blob.core.windows.net`, which serves both full GETs and `Range` requests (HTTP 206).
- OpenRoads (version 2026-04): GML entry `oproad_gml3_gb.zip`, size 608,511,751. Members are `data/OSOpenRoads_<SQ>.gml` per 100km square (SS uncompressed 142,730,630 B; ST 428,635,342 B; HP compresses 741,850 to 60,535 B, ratio 0.082). Schema: `road:RoadLink` (ns `road="http://namespaces.os.uk/Open/Roads/1.0"`) with `net:centrelineGeometry/gml:LineString/gml:posList` (ns `net="urn:x-inspire:specification:gmlas:Network:3.2"`), properties `road:roadClassification`, `road:roadFunction`, `road:formOfWay`, `road:length`, `road:loop`, `road:primaryRoute`, `road:trunkRoad`, optional `road:name1`, `road:roadClassificationNumber`. Also `road:RoadNode` (skip) and `road:MotorwayJunction` (skip).
- OpenMapLocal (2026-04): per-square zips `opmplc_gml3_<sq>.zip` (SS 48,885,253 B; ST 123,600,000 B circa, listed 117.9 MB). One member `.../data/<SQ>.gml` (SS: 361,135,570 B). Namespace `oml="http://namespaces.os.uk/open/oml/1.0"`. Feature types over SS with counts: Building 263,821; Road 66,189; SurfaceWater_Line 49,912; Woodland 29,699; NamedPlace 13,320; SurfaceWater_Area 8,067; ImportantBuilding 4,154; RailwayTrack 1,990; TidalBoundary 1,577; Foreshore 547; FunctionalSite 540; CarChargingPoint 212; Roundabout 163; ElectricityTransmissionLine 118; TidalWater 95; RailwayStation 42; RailwayTunnel 17; MotorwayJunction 13; RoadTunnel 13; Glasshouse 3. `oml:Building` carries `oml:geometry` (gml:Surface/patches/PolygonPatch/exterior/LinearRing/posList, interiors possible) + `oml:featureCode`. `oml:ImportantBuilding` adds `oml:buildingTheme` and `oml:classification`. `oml:FunctionalSite` has `oml:distinctiveName`, `oml:siteTheme`, `oml:classification`, MultiSurface geometry. `oml:RailwayTrack` has `oml:classification` + LineString.
- OpenGreenspace (2026-04): per-square zips (SS 631,663 B; ST 2.2 MB). Namespace `ogsp="http://namespaces.ordnancesurvey.co.uk/Open/Greenspace/1.0"`. `ogsp:GreenspaceSite`: `ogsp:function`, optional `ogsp:distinctiveName1` (also distinctiveName2..4 may appear), MultiSurface. `ogsp:AccessPoint`: `ogsp:accessType`, `ogsp:refToGreenspaceSite`, Point.
- OpenUPRN (2026-08): `osopenuprn_202608_csv.zip`, size 618,494,417, one member `osopenuprn_202608.csv` (2,271,141,910 B), header `UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE` with a UTF-8 BOM. Rows ordered by UPRN, not spatially.
- All GML coordinates are EPSG:27700, `srsDimension="2"`, space-separated `gml:posList` / `gml:pos`.
- Owner ground truth (Cowbridge package, 2026-08-06): OSM 1,552 building ways; Overture `_building.geojson` 2,629 features (1,076 Microsoft ML); INSPIRE 13,204 curves. The buildings fusion should push the .osm above 2,600.

## File Structure

- Create: `src/mapgen/os_downloads.py` (Downloads API client, versioned cache dirs, ranged zip reader)
- Create: `src/mapgen/os_gml.py` (streaming GML feature readers for the three vector products)
- Create: `src/mapgen/os_shards.py` (grid maths, derive-once NDJSON/CSV shard store, bbox readers)
- Create: `src/mapgen/sources/os_open.py` (OsOpenSource)
- Create: `src/mapgen/sources/os_uprn.py` (OsUprnSource)
- Create: `src/mapgen/buildings.py` (footprint fusion into the .osm)
- Create: `src/mapgen/resolver.py` (categories, tiers, resolution record)
- Modify: `src/mapgen/package.py` (register both sources; `_fuse_buildings_step`; resolution into estimate_survey and survey.json)
- Modify: `src/mapgen/sources/base.py` (document `covers`/`tier` as optional extensions)
- Modify: `src/mapgen/sources/osm.py`, `overture.py`, `elevation.py`, `lidar_wales.py`, `inspire.py` (add `covers`/`tier`)
- Modify: `src/mapgen/web/static/app.js` (tier list in the estimate panel)
- Create: `tests/test_os_downloads.py`, `tests/test_os_gml.py`, `tests/test_os_shards.py`, `tests/test_sources_os_open.py`, `tests/test_sources_os_uprn.py`, `tests/test_buildings.py`, `tests/test_resolver.py`
- Create: `tests/fixtures/osopen/` (generator scripts + committed small fixtures)
- Modify: `tests/js/test_app.js`, `tests/test_package.py`, `tests/test_web_server.py`, `tests/test_live_smoke.py`
- Modify: `README.md`, `docs/superpowers/HANDOFF.md`, `docs/urbano/README.md`

---

### Task 1: Downloads API client, versioned cache, ranged zip reader

**Files:**
- Create: `src/mapgen/os_downloads.py`
- Test: `tests/test_os_downloads.py`
- Create: `tests/fixtures/osopen/downloads_listing.json` + `tests/fixtures/osopen/make_listing_fixture.py`

**Interfaces:**
- Consumes: `cog.py`'s `ByteSource` protocol, `FileByteSource`, `HttpByteSource`, `CogError` (read their docstrings first; HttpByteSource already enforces Range support and Content-Range agreement).
- Produces (later tasks rely on these exact names):
  - `class OsOpenError(ValueError)` with attributes `kind: str` (one of `"listing"`, `"download"`, `"range"`, `"parse"`) and `status_code: int | None`. Messages NEVER contain a URL.
  - `product_downloads(product: str) -> list[dict]` : GET `{OS_DOWNLOADS_BASE}/products/{product}/downloads`, JSON-decoded; network errors wrapped as OsOpenError kind "listing" (status_code carried when known).
  - `entry_for(entries: list[dict], *, area: str, fmt: str, subformat: str | None = None) -> dict | None`
  - `product_version(product: str) -> str` : GET `{OS_DOWNLOADS_BASE}/products/{product}`, return `version` field (e.g. `"2026-04"`).
  - `download_entry(entry: dict, dest: Path, progress: ProgressSink | None = None) -> Path` : streams `entry["url"]` to `dest` atomically (`.part` then `os.replace`), verifies byte count equals `entry["size"]` (mismatch: OsOpenError kind "download"), emits progress events.
  - `class ZipReader:` constructed over a `ByteSource`; `members() -> dict[str, ZipMember]` where `ZipMember = namedtuple("ZipMember", "name method compressed_size uncompressed_size header_offset")`; `read_member(name: str) -> bytes` (range-read local header, then the compressed span, raw-deflate `zlib.decompressobj(-15)` when method is 8, stored bytes when 0; verify decompressed length equals uncompressed_size, else OsOpenError kind "range"). Handles EOCD lookup in the final 65,536 bytes; a missing EOCD signature is OsOpenError kind "range". Zip64 is out of scope: if EOCD reports 0xFFFFFFFF markers, raise OsOpenError kind "range" with a message saying the archive needs zip64, which none of the probed products do.
  - `cache_root() -> Path` : `~/.mapgen/osopen` (respecting the same home override mechanism `ensure_ostn15` uses; read bng.py first and follow its pattern exactly).
  - `product_cache_dir(product: str, version: str) -> Path` : `cache_root()/f"{product}_{version}"`.
  - `sweep_old_versions(product: str, keep_version: str) -> None` : delete sibling `f"{product}_*"` dirs other than the kept one; only after a caller has completed shards (caller's responsibility, document it).

**Steps:**

- [ ] **Step 1: Fixture generator.** Write `tests/fixtures/osopen/make_listing_fixture.py` producing `downloads_listing.json`: a trimmed but field-exact copy of the real OpenGreenspace listing (three entries: GB GML, SS GML with `{"md5": "e087721f040e06bdaad6a943c92d0539", "size": 631663, "url": "https://api.os.uk/downloads/v1/products/OpenGreenspace/downloads?area=SS&format=GML&subformat=3&redirect", "format": "GML", "subformat": "3", "area": "SS", "fileName": "opgrsp_gml3_ss.zip"}`, ST GML). Run it, commit both files.
- [ ] **Step 2: Failing tests for the client.** `entry_for` picks SS/GML and returns None for a missing area; `product_downloads` decodes a canned listing via a patched opener and wraps HTTP 503 as OsOpenError kind "listing" with status_code 503 and no URL in `str(exc)`; `download_entry` writes atomically (no `.part` left), fails on short body with kind "download". Run: `pytest tests/test_os_downloads.py -v` expecting failures ("no module named mapgen.os_downloads").
- [ ] **Step 3: Implement the client half.** urllib with a module-level opener seam (`_build_opener()` patched in tests, same pattern as `inspire.py`'s session seam; read that first). Run the tests green.
- [ ] **Step 4: Failing tests for ZipReader.** Build real zips in tmp_path with `zipfile` (one stored member, one deflated member with known content, nested `data/X.gml` names), read them through `FileByteSource`; assert `members()` names/sizes and `read_member` round-trips both compression methods; corrupt the EOCD signature and assert kind "range"; truncate a member's compressed span by patching the source and assert the length check raises.
- [ ] **Step 5: Implement ZipReader.** EOCD scan (rfind `PK\x05\x06` in tail read), central directory walk (struct unpack, 46-byte fixed header plus name/extra/comment lengths), local header read (30 bytes fixed plus lengths) to find the data start. Green.
- [ ] **Step 6: Cache dir + sweep tests and implementation.** `product_cache_dir` creates parents; `sweep_old_versions` removes only same-product siblings and survives missing root. Green.
- [ ] **Step 7: Live test.** One `@pytest.mark.live` test: `product_downloads("OpenGreenspace")` returns entries containing an SS GML entry whose fields include md5/size/url; `ZipReader` over `HttpByteSource` on the real OpenRoads GML entry lists a `data/OSOpenRoads_SS.gml` member. Run it once for real: `pytest tests/test_os_downloads.py -m live -v`.
- [ ] **Step 8: Full suite, commit.** `pytest -q` and `node --test tests/js/` per repo convention (check how the 273 Node tests are invoked in package.json or CI script and use that). `git add src/mapgen/os_downloads.py tests/test_os_downloads.py tests/fixtures/osopen/downloads_listing.json tests/fixtures/osopen/make_listing_fixture.py` then commit `feat(os-open): downloads api client, versioned cache, ranged zip reader`.

### Task 2: Streaming GML feature readers

**Files:**
- Create: `src/mapgen/os_gml.py`
- Test: `tests/test_os_gml.py`
- Create: `tests/fixtures/osopen/oml_sample.gml`, `roads_sample.gml`, `greenspace_sample.gml` + `make_gml_fixtures.py`

**Interfaces:**
- Consumes: nothing project-specific (pure stdlib); OsOpenError from Task 1 for parse failures.
- Produces:
  - `@dataclass(frozen=True) class OsFeature:` fields `feature_id: str`, `feature_type: str` (local name, e.g. `"Building"`), `geometry: dict` (GeoJSON-shaped dict with BNG coordinates: `{"type": "Point"|"LineString"|"Polygon"|"MultiPolygon", "coordinates": [...]}`), `properties: dict[str, object]`.
  - `iter_oml_features(fh) -> Iterator[OsFeature]` : yields the feature types listed in `OML_TYPES` (exact map below), skipping all others.
  - `iter_road_features(fh) -> Iterator[OsFeature]` : yields RoadLink only.
  - `iter_greenspace_features(fh) -> Iterator[OsFeature]` : yields GreenspaceSite and AccessPoint.

`OML_TYPES` and the properties each carries (None when the element is absent):
- `Building`: `{"code": featureCode}`
- `ImportantBuilding`: `{"code": featureCode, "theme": buildingTheme, "class": classification}`
- `FunctionalSite`: `{"name": distinctiveName, "theme": siteTheme, "class": classification}`
- `Woodland`, `SurfaceWater_Area`, `SurfaceWater_Line`, `TidalWater`, `Foreshore`: `{"code": featureCode}`
- `RailwayTrack`, `RailwayTunnel`: `{"class": classification}` (tunnel may have none; then `{"code": featureCode}`)
- `RailwayStation`, `NamedPlace`: `{"name": distinctiveName, "class": classification}` (NamedPlace: read the fixture generator's captured sample; if its name element differs, follow the real file and record the deviation in the task report)
- RoadLink properties: `{"class": roadClassification, "function": roadFunction, "form": formOfWay, "name": name1, "number": roadClassificationNumber, "trunk": trunkRoad == "true", "primary": primaryRoute == "true", "length": float(length)}`
- GreenspaceSite: `{"function": function, "name": distinctiveName1}`; AccessPoint: `{"access": accessType, "site": refToGreenspaceSite}`

**Steps:**

- [ ] **Step 1: Fixture generator.** `make_gml_fixtures.py` writes the three sample GML files using the exact namespaces and element structure from the verified-facts section (copy the real samples embedded there: a Building with one exterior ring, an ImportantBuilding, a FunctionalSite with MultiSurface AND one interior ring (add a hole to the real sample), a Woodland, a RailwayTrack LineString, a NamedPlace point, two RoadLinks (one with name1 + roadClassificationNumber, one without), a GreenspaceSite + AccessPoint). Every posList coordinate pair inside 280000-320000 easting, 160000-200000 northing (Vale of Glamorgan range). Run it, commit generator + outputs.
- [ ] **Step 2: Failing tests.** Feature counts per type; a Building polygon's exterior ring closes and coordinates match the fixture verbatim; the FunctionalSite hole arrives as a second ring; RoadLink trunk/primary parse to bool, absent name1 arrives as None; AccessPoint yields a Point; a truncated GML (cut the fixture bytes in half) raises OsOpenError kind "parse" (never a raw ParseError); memory discipline: after iterating a fixture, the root element has no retained featureMember children (assert via parsing wrapper internals or peak RSS is out of scope; test the `elem.clear()` call path by asserting the iterator yields lazily from a generator, marker: `inspect.isgeneratorfunction`).
- [ ] **Step 3: Run tests, expect import failure.**
- [ ] **Step 4: Implement.** `ET.iterparse(fh, events=("end",))`, dispatch on local name inside the product namespace, shared `_geometry(elem)` handling Point/LineString/Surface/MultiSurface with interior rings, `root.clear()` per `os:featureMember` (INSPIRE's exact discipline; read `inspire.py`'s parser first and mirror its structure). Wrap `ET.ParseError` as OsOpenError kind "parse".
- [ ] **Step 5: Green, then real-file spot check.** One `@pytest.mark.live`-free but slow-safe test is NOT wanted here; instead verify by hand against the probe download if present (optional, not a test). Full suite.
- [ ] **Step 6: Commit** `feat(os-open): streaming gml readers for openmap local, open roads, greenspace`.

### Task 3: Grid maths and the derive-once shard store

**Files:**
- Create: `src/mapgen/os_shards.py`
- Test: `tests/test_os_shards.py`
- Create: `tests/fixtures/osopen/uprn_sample.csv` + generator appended to `make_gml_fixtures.py` or its own `make_uprn_fixture.py`

**Interfaces:**
- Consumes: `OsFeature` from Task 2; OsOpenError from Task 1.
- Produces:
  - `grid_square(easting: float, northing: float) -> str` : the two-letter 100km GB square (e.g. 299000,179000 -> "SS"; 301000,179000 -> "ST"). Standard OS algorithm: 500km letter from (easting//500000, northing//500000), 100km letter from the remainder; letters skip I.
  - `squares_for(e_min, n_min, e_max, n_max) -> list[str]` : sorted unique squares intersecting the rectangle (walk the 100km lattice).
  - `cell_10km(easting: float, northing: float) -> str` : e.g. `"SS97"` (square + easting digit + northing digit of the 10km cell).
  - `cells_for(e_min, n_min, e_max, n_max) -> list[str]`.
  - `write_shards(features: Iterable[OsFeature], shard_dir: Path) -> dict` : streams features into `shard_dir/<CELL>.ndjson.gz` (one JSON object per line: `{"id", "type", "geometry", "properties"}`), a feature written into EVERY cell its geometry bbox intersects; writes `meta.json` LAST with `{"total": n, "cells": {cell: count}}`; returns the meta dict. Partial output without meta.json means incomplete (the INSPIRE resume-marker lesson: completion is meta.json's existence, and `shards_complete` must refuse a dir where meta.json exists but a listed cell file is missing, or cell files exist without meta).
  - `shards_complete(shard_dir: Path) -> bool` (the biconditional above).
  - `features_in(shard_dir: Path, e_min, n_min, e_max, n_max) -> Iterator[dict]` : reads only intersecting cells, dedups by `id` across cells, yields only features whose geometry bbox intersects the query rectangle.
  - `write_uprn_shards(text_stream, shard_dir: Path) -> dict` : csv.reader over the stream (open with `utf-8-sig` semantics: strip a leading BOM), rows `(UPRN, X, Y, LAT, LON)` written to `shard_dir/<SQ>.csv.gz` per 100km square, meta.json LAST `{"total": n, "squares": {sq: count}}`.
  - `uprn_in(shard_dir: Path, e_min, n_min, e_max, n_max) -> Iterator[tuple[int, float, float, float, float]]`.

**Steps:**

- [ ] **Step 1: Failing tests for grid maths.** Exact anchors: `grid_square(299000, 179000) == "SS"`, `grid_square(301000, 179000) == "ST"`, `grid_square(451000, 1215000) == "HP"` (matches the probed OpenRoads member for Shetland), `cell_10km(297393, 100106) == "SS90"` (from the real GreenspaceSite sample: easting 297393 -> digit 9, northing 100106 -> digit 0), `squares_for` across the SS/ST seam returns both.
- [ ] **Step 2: Implement grid maths, green.** Derive letters arithmetically (the false-origin walk), not a lookup table you cannot defend; add the derivation as a comment citing the OS National Grid definition.
- [ ] **Step 3: Failing tests for shards.** Round-trip: write 5 OsFeatures across two cells (one polygon straddling both), read back with `features_in` over a bbox covering both cells and assert the straddler arrives once (dedup); a bbox covering one cell excludes the other cell's features; `shards_complete` false when meta.json absent, false when meta lists a missing cell, false when cells exist but meta absent, true on the happy path; killing the writer mid-way (raise injected after first cell) leaves no meta.json.
- [ ] **Step 4: Implement shard store, green.** Open all cell writers lazily (dict of gzip handles), close all before meta write, `os.replace` for meta atomicity.
- [ ] **Step 5: UPRN variant.** Fixture CSV with BOM + header + 6 rows straddling SS/ST (include the real first row `1,358260.99,172796.83,51.4526038,-2.6020703`). Failing tests: shard split by square, BOM stripped, bbox read returns only in-range rows, ints/floats typed. Implement, green.
- [ ] **Step 6: Full suite, commit** `feat(os-open): grid maths and derive-once shard store`.

### Task 4: OsOpenSource

**Files:**
- Create: `src/mapgen/sources/os_open.py`
- Modify: `src/mapgen/package.py` (register in `register_default_sources` tuple, line ~321)
- Test: `tests/test_sources_os_open.py`; Modify: `tests/test_package.py` (registration + possible_outputs sweeps), `tests/test_live_smoke.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3; `bng.py` `ensure_ostn15`, `load_ostn15`, `to_bng`, `from_bng`, `padded_bng_extent`; `sources/base.py` conventions (`tile_failures`, `possible_outputs`, progress events); read `sources/inspire.py` end to end first, it is the closest sibling.
- Produces:
  - `class OsOpenSource:` id `"os_open"`, display_name `"OS Open map data (GB)"`, licence `"Open Government Licence v3.0"`, attribution `"Contains OS data © Crown copyright and database right [year]"` (the literal `[year]` placeholder in the class attribute; merge substitutes), requires_api_key False.
  - `PRODUCTS = ("OpenMapLocal", "OpenRoads", "OpenGreenspace")`.
  - Estimate constants, each with a dated comment: `OML_BYTES_PER_SQUARE = 120_000_000` (SS zip 48.9 MB, ST 121.5 MB measured 2026-08-07; national squares vary, thin evidence), `ROADS_BYTES_PER_SQUARE = 40_000_000` (ST member 428.6 MB uncompressed at the HP-measured 0.082 deflate ratio is ~35 MB, headroom added), `GREENSPACE_BYTES_PER_SQUARE = 3_000_000` (SS 0.6 MB, ST 2.2 MB), `BYTES_PER_SECOND_ESTIMATE = 1_800_000.0` (inspire-measured floor, 2026-08-06), `SECONDS_FLOOR = 3.0` (placeholder until Task 9 refits from a live run; say so in the comment).
  - `estimate(bbox, tiles)` : squares from `padded_bng_extent` (pad 200.0, matching the package pad; confirm the exact pad value the pipeline uses by reading `package.py`'s INSPIRE/LiDAR calls and use the same constant source), per product per square: 0 bytes if that product's current CACHED shards exist on disk (`shards_complete` on any version dir for the product, no network), else the constant. OSTN15 grid absent adds `OSTN15_BYTES` the way `lidar_wales.estimate` does (read it; reuse its constant if importable, else mirror it with a comment naming the origin).
  - `fetch(bbox, tiles, work_dir, progress, cancel=None)` : ensure OSTN15; per product: `product_version()` (listing unreachable: fall back to the newest complete cached version dir with a progress note event `os_open_cache_fallback`; no cache either: raise OsOpenError kind "listing"); ensure shards per needed square: OpenMapLocal and OpenGreenspace download the square zips via `download_entry` into `<cache>/raw/`, open with zipfile, parse the `.gml` member with the Task 2 reader streaming into `write_shards` under `<cache>/shards/<SQ>/`, delete the raw zip after meta lands; OpenRoads constructs `ZipReader(HttpByteSource(entry url...))` wait, HttpByteSource takes a URL: build it over the LISTED entry url (it follows the redirect; verify HttpByteSource follows redirects by reading cog.py, and if it does not, resolve the final URL once with a HEAD/GET via the opener and hand HttpByteSource that; NEVER put either URL in an error message), reads `data/OSOpenRoads_<SQ>.gml` members, spools each to a temp file under `<cache>/raw/`, parses and shards the same way. `sweep_old_versions` after each product completes. Then write work parts: for each product one `work_dir/os_open_<product>.ndjson` holding the bbox-filtered features from `features_in`. `tile_failures` records per (product, square) failures with reasons free of URLs; continue past failures, raise once at the end (base.py convention). Respect `cancel` between (product, square) units.
  - `merge(parts, out_dir, stem)` : reads the work parts, transforms BNG to WGS84 via `from_bng` (grid loaded via `load_ostn15`; a missing grid at merge time is an error, matching fetch's guarantee), and writes exactly these files (only those with at least one feature; record zero-feature categories in the returned record semantics documented in Task 6's survey wiring):
    - `<stem>_os_buildings.geojson` : Building + ImportantBuilding + Glasshouse polygons, properties `{"source": "os_openmap_local", "code": ..., "theme": ..., "class": ...}` (absent keys omitted).
    - `<stem>_os_roads.geojson` : RoadLink lines, properties `{"source": "os_open_roads", "class", "function", "form", "name", "number", "trunk", "primary"}`.
    - `<stem>_os_rail.geojson` : RailwayTrack + RailwayTunnel lines `{"source": "os_openmap_local", "class"}`.
    - `<stem>_os_greenspace.geojson` : GreenspaceSite polygons + AccessPoint points `{"source": "os_open_greenspace", "function"/"access", "name"}`.
    - `<stem>_os_sites.geojson` : FunctionalSite + RailwayStation + NamedPlace `{"source": "os_openmap_local", "theme", "class", "name"}`.
    - `<stem>_os_land.geojson` : Woodland + SurfaceWater_Area + SurfaceWater_Line + TidalWater + Foreshore `{"source": "os_openmap_local", "kind": "woodland"|"water_area"|"water_line"|"tidal_water"|"foreshore"}`.
    Attribution `[year]` substitutes from the product version's year (e.g. "2026-04" -> "2026"): merge returns paths; the survey.json provenance enrichment happens in package.py (Task 6 wires the record; follow `_enrich_inspire_provenance` as the model).
  - `possible_outputs(stem)` : the six names above.

**Steps:**

- [ ] **Step 1: Read first.** `sources/inspire.py` whole, `sources/base.py` docstring whole, `package.py` `_configured_sources` + `register_default_sources` + the INSPIRE fusion step. List in the report which pad constant the pipeline uses for INSPIRE and use the same one.
- [ ] **Step 2: Failing tests, estimate.** Never touches network (a poisoned opener seam that raises on any call); prices three products x squares when cache empty; prices 0 for a product whose fake shard dir has complete meta; includes OSTN15 bytes when grid absent.
- [ ] **Step 3: Implement estimate, green.**
- [ ] **Step 4: Failing tests, fetch.** With patched opener + canned listings + fixture zips built in tmp (real zipfile bytes served through the seam): shards get built per square, raw zips deleted, meta lands last, work parts contain only bbox-intersecting features, a 503 on one square lands in tile_failures (no URL in reason) while the other square completes, cache fallback path emits the event when listing raises but cache is complete, second fetch with warm cache downloads nothing (poisoned opener proves it).
- [ ] **Step 5: Implement fetch, green.** OpenRoads member path uses ZipReader over the byte source; keep the redirect resolution inside a helper with a docstring stating the no-URL-in-errors rule.
- [ ] **Step 6: Failing tests, merge.** Fixture work parts -> exactly the six files with stems embedded, WGS84 coordinates (assert one known BNG pair converts to the OSTN15-correct lon/lat within 1e-6 using the test grid slice from `tests/fixtures/ostn15/`), properties exact per the map above, zero-feature product yields no file.
- [ ] **Step 7: Implement merge, green.**
- [ ] **Step 8: Registration.** Add to `register_default_sources` tuple; `tests/test_package.py` gains: registered by default, type-checked duplicate skip holds, `possible_outputs` participates in the stale-output sweep. Green.
- [ ] **Step 9: Live test** (`@pytest.mark.live`): tiny extent over Cowbridge (bbox from the verified-facts section), fetch OpenGreenspace ONLY via a configured single-product instance? No: fetch all three but assert wall time under 15 minutes and print measured bytes/seconds per product to stdout for Task 9's refit (first run downloads OML SS+ST ~170 MB and roads members ~50 MB; the machine's measured 1.8 MB/s makes that ~2-3 minutes each). Assert the six outputs exist and `_os_buildings.geojson` feature count > 500 for the Cowbridge extent.
- [ ] **Step 10: Full suite, commit** `feat(os-open): os open source, three products, sharded fetch and geojson merge`.

### Task 5: OsUprnSource

**Files:**
- Create: `src/mapgen/sources/os_uprn.py`
- Modify: `src/mapgen/package.py` (registration)
- Test: `tests/test_sources_os_uprn.py`; Modify: `tests/test_package.py`

**Interfaces:**
- Consumes: Tasks 1 and 3; bng.py.
- Produces: `class OsUprnSource:` id `"os_uprn"`, display_name `"Addresses (OS Open UPRN, GB)"`, licence OGL v3, attribution as Task 4, requires_api_key False.
  - `UPRN_BYTES_ONE_TIME = 618_494_417` (exact listed size, 2026-08 version, probed 2026-08-07).
  - `routing_note()` : when no complete UPRN shard dir exists, return `"Addresses: first use downloads the national OS Open UPRN file (619 MB, cached for every later survey)."`; else None. (This surfaces in the estimate warnings automatically; read `estimate_survey`'s routing_note convention.)
  - `estimate` : cached -> `SECONDS_FLOOR` only; uncached -> the one-time bytes at the measured rate.
  - `fetch` : ensure shards once per version (download the CSV zip to `<cache>/raw/` atomically, stream the member through `write_uprn_shards`, meta last, delete the raw zip; peak disk ~1.3 GB, steady ~600 MB of gzipped shards; put those numbers in the class docstring); work part: bbox rows as CSV.
  - `merge` : `<stem>_os_uprn.geojson`, Point features `{"uprn": int, "source": "os_open_uprn"}` using the LATITUDE/LONGITUDE columns directly (they are OS-computed WGS84; do NOT re-derive from easting/northing, and say why in a comment: the columns are authoritative and avoid a transform disagreement).
  - `possible_outputs(stem)` : that one name.

**Steps:**

- [ ] **Step 1: Failing tests.** estimate honesty both ways; routing_note both ways; fetch shards from a fixture zip through the opener seam (zip containing a BOM'd CSV member), raw zip deleted, meta last; warm-cache fetch does zero network (poisoned opener); merge produces points with int uprn; work part rows only inside bbox.
- [ ] **Step 2: Implement, green.**
- [ ] **Step 3: Registration + package tests, green.** (Deliberately NOT in any default selection; opt-in checkbox comes free from the registry, mirroring LidarWalesSource's registration note.)
- [ ] **Step 4: Live test:** `@pytest.mark.live` AND skipif no complete UPRN cache exists (`pytest.skip("national UPRN cache not present; run a real survey with os_uprn selected to build it")`): with cache present, `uprn_in` over the Cowbridge bbox yields > 1000 rows. The 619 MB download is never triggered by the test suite.
- [ ] **Step 5: Full suite, commit** `feat(os-open): uprn address source, one-time national shard`.

### Task 6: Buildings fusion into the .osm

**Files:**
- Create: `src/mapgen/buildings.py`
- Modify: `src/mapgen/package.py` (`_fuse_buildings_step`, ordering, survey.json record, bridge_package parity)
- Test: `tests/test_buildings.py`; Modify: `tests/test_package.py`

**Interfaces:**
- Consumes: `heights.py`'s `_rewrite_osm` (heights.py:319) and `_declaration_line` (heights.py:279); `package.py`'s `_minimum_existing_id` (package.py:1756); the boundaries fusion step as the structural model (find `_fuse_boundaries_step` in package.py and mirror its shape: record keys always present, events, idempotency, presence in run_survey AND bridge_package).
- Produces:
  - `fuse_missing_buildings(osm_path: Path, candidates: Sequence[tuple[str, list[dict]]]) -> BuildingsFusionRecord` where each tuple is `(source_tag, geojson_features)` in priority order and the record is `@dataclass class BuildingsFusionRecord: written: int; per_source: dict[str, int]; skipped_overlap: int; kept_existing: int` (kept_existing = count of building ways already in the file).
  - package.py `_fuse_buildings_step(paths, record_sink, progress)` reading `<stem>_building.geojson` (Overture, source_tag `"overture"`) then `<stem>_os_buildings.geojson` (source_tag `"os_openmap_local"`), each optional; survey.json key `"buildings_fusion"` with keys ALWAYS `{"written", "from_overture", "from_os", "kept_existing", "skipped_overlap", "error"}`; events `buildings_fusion_started/finished/failed`.

**Fusion rule (exact):** A candidate footprint is injected only if BOTH hold: (a) its centroid (arithmetic mean of exterior-ring vertices, last-equals-first vertex dropped) lies inside no accepted footprint (existing OSM building ways resolved through their node refs, plus already-accepted candidates), and (b) no accepted footprint's centroid lies inside the candidate. Point-in-polygon by ray casting on the exterior ring. A spatial hash over lon/lat with 0.0005 degree cells keeps it linear; a candidate only tests polygons sharing a cell with its centroid or bbox. Candidates with a `height` property carry it as a `height=` tag (stringified as OSM convention, metres, one decimal). Injected ways: `building=yes` (or the candidate's own building/class property when it is a non-empty string), `source=<source_tag>`, node ids and way ids strictly below `_minimum_existing_id`, nodes deduplicated within the injection (a shared corner is still two nodes across two ways; do not conflate, it is not worth the machinery, say so in a comment). MultiPolygon candidates: inject the exterior of each polygon as its own way, holes dropped, with a comment naming the simplification and the counts of affected features in the record's report (holes in building footprints are vanishingly rare at these scales).

**Ordering (exact):** `_fuse_buildings_step` runs BEFORE the LiDAR heights fusion step in both run_survey and bridge_package, so injected footprints without heights get DSM-DTM heights when lidar_wales ran. Read the current step order around the existing boundaries fusion call sites and place buildings fusion ahead of heights fusion; state the final order in the task report.

**Idempotency:** a file whose building ways already carry `source=overture` or `source=os_openmap_local` tags is treated exactly the way boundaries fusion treats `source=hm_land_registry`: those ways are stripped and re-fused from the current inputs (read how `_fuse_boundaries_step` actually does it FIRST; if it skips instead of re-fusing, mirror the skip and record it; consistency with the sibling step beats this paragraph).

**Steps:**

- [ ] **Step 1: Read the three consumed functions and the boundaries step.** Report their actual behaviour before writing tests.
- [ ] **Step 2: Failing tests, geometry core.** Ray cast: inside, outside, vertex-touch (document the chosen convention with strict inequality, matching contours.py's crossing convention), centroid of a square; spatial hash returns candidate sets across cell borders.
- [ ] **Step 3: Failing tests, fusion.** Build a small .osm (three buildings, ids mixed positive/negative to exercise `_minimum_existing_id`) plus candidates: one Overture duplicate of an OSM building (skipped by rule a), one Overture new (written, height tag carried), one OS duplicate of the accepted Overture one (skipped: tests candidate-vs-candidate dedup), one OS new (written), one OS new whose centroid sits inside an OSM courtyard-shaped... no: keep the L-shaped case OUT (holes dropped); instead one MultiPolygon candidate (two exteriors -> two ways). Assert per_source counts, way ids below minimum, node count, height tag format "7.4".
- [ ] **Step 4: Implement, green, mutation-check the dedup.** Flip rule (a) to test centroids against candidates only (not OSM ways) and assert a test fails; flip the ray cast inequality and assert a test fails. (Machine traps: bump pyc mtimes, use binary-safe patterns, never `git checkout --` uncommitted work.)
- [ ] **Step 5: Failing tests, package step.** Record keys always present; missing both inputs -> written 0, no error; Overture-only run works (os_open unselected: the owner's default case TODAY); events emitted; idempotent double run; bridge_package parity; ordering: heights fusion sees injected ways (integration test: run buildings then heights steps over a fixture with a fake DSM sampler and assert an injected way gained height, if the heights step's seams allow it cheaply; if not, assert relative order of step calls via a recording progress sink and say why).
- [ ] **Step 6: Implement step, green.**
- [ ] **Step 7: Full suite, commit** `feat(buildings): fuse overture and os footprints the osm base lacks`.

### Task 7: The tier resolver

**Files:**
- Create: `src/mapgen/resolver.py`
- Modify: `src/mapgen/sources/base.py` (document `covers`/`tier` in the LayerSource docstring's optional-extension list), `osm.py`, `overture.py`, `elevation.py`, `lidar_wales.py`, `inspire.py`, `os_open.py`, `os_uprn.py` (implement them), `package.py` (estimate_survey + run_survey wiring)
- Test: `tests/test_resolver.py`; Modify: `tests/test_web_server.py` (estimate payload), `tests/test_package.py` (survey.json)

**Interfaces:**
- Produces:
  - Source extension methods (optional, read defensively, the codebase's own convention): `covers(bbox: BBox) -> str` returning `"full" | "partial" | "none"`, and `tier(category: str) -> int | None` (None = does not serve it). The spec text says `covers -> bool | partial`; three string states implement that intent honestly (Wales mosaic edges are genuinely partial); note the divergence in resolver.py's docstring.
  - Per-source implementations: osm/overture/elevation return "full" always (global sources). lidar_wales: full/partial/none from MOSAIC_BOUNDS intersection. inspire: full when every corner falls in some indexed authority bbox, partial when some, none otherwise (reuse `authorities_for`). os_open/os_uprn: full when all needed 100km squares are real GB squares that the product serves, partial when some, none when none (OpenMapLocal's area list from the committed fixture has 56 squares; commit that set as a constant `GB_SQUARES` in os_shards.py during this task, generated from the probe listing, with the generator committed).
  - Tier tables (1 = best per-feature quality), as each source's `tier()`: terrain: lidar_wales 1, elevation 2. contours: lidar_wales 1. heights: lidar_wales 1, overture 2. buildings: osm 1, overture 2, os_open 3. roads: osm 1, os_open 2. rail: osm 1, os_open 2. boundaries: inspire 1. greenspace: os_open 1, overture 2, osm 3. sites: os_open 1. land: os_open 1, overture 2. addresses: os_uprn 1. land_use: overture 1, osm 2. water: overture 1, os_open 2. places: overture 1, os_open 2.
  - `resolver.py`: `CATEGORIES` tuple (the fourteen above, that order); `ROLES: dict[tuple[str, str], str]` overrides mapping (category, source_id) to `"reference"` for (roads, os_open), (rail, os_open); everything else derives role: best tier present = `"base"`, others `"fill"`. `resolve(bbox, sources) -> list[dict]`: per category, the covering (`covers != "none"`) sources that serve it, sorted by tier, each entry `{"id", "display_name", "tier", "coverage", "role"}`; categories with no covering source are omitted.
  - `estimate_survey` gains `result["resolution"] = resolve(request.bbox, configured_sources)`; run_survey records the same list in survey.json under `"resolution"` (compute it once at plan time from the same configured sources, not twice).

**Steps:**

- [ ] **Step 1: Failing tests, covers.** Cardiff bbox: lidar_wales full, inspire full, os_open full. A mid-Wales/England border bbox spanning the mosaic edge: lidar_wales partial. Paris bbox: lidar_wales none, inspire none, os_open none (no GB squares), osm/overture/elevation full. Edinburgh bbox: inspire none (Scotland), os_open full.
- [ ] **Step 2: Implement covers per source, green.** GB_SQUARES generator + committed constant included.
- [ ] **Step 3: Failing tests, resolve.** Cardiff with all sources selected: buildings lists osm(1, base) overture(2, fill) os_open(3, fill); roads lists osm base + os_open reference; addresses only os_uprn; Paris with the same selection: buildings lists only osm+overture, boundaries/greenspace/sites/addresses absent. Selection matters: os_open unselected removes its entries.
- [ ] **Step 4: Implement resolver + wiring, green.** estimate response asserted in test_web_server; survey.json key asserted in test_package.
- [ ] **Step 5: Full suite, commit** `feat(resolver): per-extent category tiers in estimate and survey.json`.

### Task 8: Tier list in the web UI

**Files:**
- Modify: `src/mapgen/web/static/app.js`, `src/mapgen/web/static/index.html` (a container element in the estimate panel if one is needed), CSS only if the existing panel styles do not cover a small list
- Test: `tests/js/test_app.js`

**Interfaces:**
- Consumes: the estimate payload's `resolution` list from Task 7.
- Produces: a `renderResolution(resolution)` function called from `refreshEstimate`'s success path (find where warnings render and place the tier list in the same panel, after warnings: this is the availability-watcher slot the spec names), and cleared wherever warnings are cleared.

**Copy rules (exact):** one line per category: `<category>: <base display name>` then `, filled by <name>` per fill entry, then `, reference: <name>` per reference entry. Coverage `"partial"` appends ` (partial coverage here)` to that entry's name. No em dashes. Category labels humanised: `land_use` renders "land use", `heights` renders "building heights", `land` renders "woodland and water", `sites` renders "functional sites", `places` renders "place names". Others render as-is.

**Steps:**

- [ ] **Step 1: Read tests/js/test_app.js's harness pattern and refreshEstimate.** Report how the existing 273 tests drive DOM and fetch stubs; follow it exactly.
- [ ] **Step 2: Failing tests.** renderResolution builds the list (assert exact text for a two-category fixture including a partial and a reference entry); empty/absent resolution renders nothing and clears any previous list; refreshEstimate path populates it from a stubbed estimate response; a new estimate replaces the old list.
- [ ] **Step 3: Implement, green.** Full Node suite + full Python suite (server tests already cover the payload).
- [ ] **Step 4: Commit** `feat(web): tier list in the estimate panel`.

### Task 9: Live proof, constants refit, docs

**Files:**
- Modify: `src/mapgen/sources/os_open.py` + `os_uprn.py` (constants, only from measurements), `README.md` (source table + OS Open section, OGL attribution, the deferred Boundary-Line note), `docs/superpowers/HANDOFF.md` (counts, next item, watch items), `docs/urbano/README.md` (the six new GeoJSON files and the owner's key/value import workflow for them)
- Test: `tests/test_live_smoke.py` (one full-pipeline live case)

**Steps:**

- [ ] **Step 1: Live full run.** `@pytest.mark.live` end-to-end: small Cowbridge-area extent, sources osm+overture+os_open (+os_uprn ONLY if its cache already exists on this machine), assert: the six os_open files land, `buildings_fusion.written > 400` (Cowbridge ground truth says Overture alone adds ~1,000 over the full town; a small extent scales down), survey.json has `resolution` with buildings/roads/greenspace entries, `.osm` building-way count strictly greater than before fusion. Record wall time and bytes per product printed to stdout.
- [ ] **Step 2: Refit constants.** Replace SECONDS_FLOOR and any per-square byte constant that the measurement contradicts, each with the dated measurement in a comment (the item 1 and 2 discipline: every constant traceable to a number someone measured on a date).
- [ ] **Step 3: Docs.** README source table gains os_open + os_uprn rows with OGL attribution and the one-time-download honesty (619 MB UPRN, ~170 MB OpenMapLocal for SS+ST); Urbano README documents the six files and that `_os_roads.geojson` is a REFERENCE layer (deliberately not fused into the .osm: it would double every road; the owner filters it by key/value in Urbano's GeoJSON import); HANDOFF's "Then" points at item 4 (constraints/Cadw/planning.data.gov.uk) plus the owner-approved addendum (categorised boundaries, roofs, canopy, GH GeoTIFF script) and the benchmark item; carry forward the parked minors (Retry-After now has three would-be consumers; the inspire self-heal unlink scope; merge-cost estimate terms).
- [ ] **Step 4: Full suites, commit** `docs(os-open): live-proven constants and package documentation`.

## Self-review notes

- Spec coverage: sources row 3 (four of five products; Boundary-Line deferral flagged to the owner in the header), resolver + covers/tier + survey.json record + UI tier list (spec Architecture section), fusion-only (replacement stays deferred), OGL attribution recorded. The owner's missing-buildings fix is Task 6 and works with or without os_open selected.
- Type consistency: OsFeature flows Task 2 -> 3 -> 4; shard dirs Task 3 -> 4/5; six output filenames Task 4 -> 6 (os_buildings input) -> 9 (docs); resolution list Task 7 -> 8.
- No placeholders: every constant is a probed number with its date; the two deliberate deferrals (SECONDS_FLOOR refit, NamedPlace name element) name the task that resolves them.
