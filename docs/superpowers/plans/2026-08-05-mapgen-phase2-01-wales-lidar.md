# Phase 2, item 1: Wales LiDAR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `lidar_wales` layer source that pulls site-sized windows from the whole-Wales 1 m LiDAR COG mosaics and delivers the three things the spec promises from one source: contour polylines per drawing scale, DSM minus DTM heights written onto the buildings OSM left flat, and a `.egrid` built from 1 m data instead of 30 m.

**Architecture:** Four new leaf modules (`bng.py` datum transform, `cog.py` remote Cloud Optimized GeoTIFF reader, `contours.py` marching squares, `geotiff_write.py` raster writer), one new source module wired through the existing `LayerSource` registry, and two `package.py` post-steps (height fusion, egrid source preference) that run in both `run_survey` and `bridge_package`. Everything is stdlib plus the `requests` dependency the project already has.

**Tech Stack:** Python stdlib (`struct`, `zlib`, `zipfile`, `array`, `math`, `xml.etree`), `requests` (existing), pytest with the repo's existing `live` marker convention for network tests.

**Spec:** `docs/superpowers/specs/2026-08-05-mapgen-phase2-design.md`, build order item 1. Evidence base: `.superpowers/sdd/2026-08-01-mapgen-phase1/phase2-task-zero-report.md` claim 1, plus the live probe recorded below.

## Measured facts this plan is built on (probed live 2026-08-05)

The two 32 bit mosaics, fetched by HTTP range request from
`https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dtm_32bit_cog.tif` (48,611,310,928 bytes) and
`https://dmwproductionblob.blob.core.windows.net/cogs/lidar/wales_dsm_32bit_cog.tif` (52,096,926,263 bytes):

- **BigTIFF** (version word 43, 8-byte offsets), little endian. The classic-TIFF reader in `geotiff.py` cannot open these and must not be bent to; the remote reader is a new module.
- Full resolution 191,007 x 233,000 pixels at exactly 1.0 m. ModelTiepoint places pixel (0,0) at easting 164,993, northing 397,000, so the mosaic spans E 164,993..356,000 and N 164,000..397,000.
- **EPSG:27700** (GeoKey 3072 = 27700, model type 1 = projected, linear units 9001 = metre). GeoAscii: "OSGB36 / British National Grid".
- **RasterPixelIsArea** (GeoKey 1025 = 1). Phase 1's reader refuses this convention; here the pixel CENTRE of (col, row) is at `(164993 + col + 0.5, 397000 - row - 0.5)`. Getting this wrong shifts every height half a metre diagonally, which renders rather than failing.
- Compression 8 (**deflate**), predictor 1 (**none**). `zlib.decompress` is the whole codec. No LZW, no floating point predictor.
- Sample format 3, 32 bits: IEEE float32. **GDAL_NODATA = "-9999"** (sea, England, and unflown gaps).
- Tiled **256 x 256**; full resolution has 747 x 911 = 680,517 tiles. TileOffsets is type 16 (LONG8, 8 bytes each, 5.4 MB total) and TileByteCounts is type 4 (LONG, 4 bytes each, 2.7 MB): the reader must range-read the *slices* of these arrays it needs, never the whole arrays.
- **Seven IFD levels**: full resolution plus six overviews, each halving (level L has pixel size 2^L metres; level 1 is 95,504 x 116,500, level 6 is 2,985 x 3,641). Every level is deflate, 256 x 256 tiles, same nodata. DTM and DSM have identical structure.
- The 16 bit variants exist (3.6 GB, Int16); this plan uses the 32 bit floats only.

OSTN15 (probed the same day): the developers pack is a stable public zip at
`https://www.ordnancesurvey.co.uk/documents/resources/OSTN15-OSGM15-DevelopersPack.zip`
(15,499,083 bytes). Contents include `OSTN15_OSGM15_DataFile.txt` (41,095,327 bytes, the 1 km shift grid),
`OSTN15_OSGM15_TestInput_ETRStoOSGB.txt`, `OSTN15_OSGM15_TestOutput_ETRStoOSGB.txt`,
`OSTN15_OSGM15_TestInput_OSGBtoETRS.txt`, `OSTN15_OSGM15_TestOutput_OSGBtoETRS.txt`
(OS's own authoritative test vectors), `OSGM15_Notice_of_release_for_developers.pdf` (the licence notice),
and `Transformations_and_OSGM15_User_Guide.pdf` (the algorithm, with a worked example).

## Why OSTN15 and not a Helmert transform

The mosaic is in OSGB36 British National Grid; mapgen's world is WGS84. The single national Helmert transformation is out by roughly 2 to 3.5 m depending on where you stand, which against 1 m LiDAR would shift every contour a visible distance off the OSM streets beside it and sample building heights across the wrong wall. OSTN15 is Ordnance Survey's definitive transformation (about 0.1 m), it is a plain data file under a free-to-use developer licence, and it is the georeference backbone every later phase 2 source (INSPIRE GML, OS Open, DataMapWales constraints) will need, because they are all EPSG:27700 too. Building it once here is not gold-plating item 1; it is the foundation of items 2 through 8. The residual WGS84-vs-ETRS89 plate drift (under 1 m in 2026) is accepted and documented, the same way every UK mapping consumer accepts it.

## File structure

| File | Responsibility |
|---|---|
| `src/mapgen/bng.py` (new) | WGS84/ETRS89 to British National Grid and back: GRS80 transverse Mercator + OSTN15 shift grid (fetch, cache, parse, interpolate) |
| `src/mapgen/cog.py` (new) | Remote and local Cloud Optimized GeoTIFF windows: BigTIFF/classic IFD chains, level selection, range-read tile fetch, deflate decode, `BngWindow` with bilinear sampling |
| `src/mapgen/contours.py` (new) | Marching squares over a `BngWindow`, polyline joining, per-scale interval policy, GeoJSON output |
| `src/mapgen/geotiff_write.py` (new) | Write a `BngWindow` as a tiled deflate float32 EPSG:27700 GeoTIFF that `cog.py` reads back |
| `src/mapgen/sources/lidar_wales.py` (new) | The `LayerSource`: estimate, fetch (OSTN15 ensure + two COG windows into work_dir), merge (packaged rasters + contour files) |
| `src/mapgen/heights.py` (new) | DSM minus DTM per building footprint, fused into the merged `.osm` as `height` tags |
| `src/mapgen/package.py` (modify) | Register the source; height-fusion post-step; egrid step prefers the LiDAR DTM; survey.json blocks |
| `src/mapgen/egrid.py` (modify) | `write_elevation_grid_from_sampler` entry point so the grid can be fed by a sampler chain |
| `tests/test_bng.py`, `tests/test_cog.py`, `tests/test_contours.py`, `tests/test_geotiff_write.py`, `tests/test_lidar_wales.py`, `tests/test_heights.py` (new) | Per-module suites; live tests carry the repo's existing `live` marker and stay deselected by default |
| `tests/fixtures/ostn15/` (new) | Three OS test stations' inputs, expected outputs, and the minimal shift-grid slice that covers them |

## Global Constraints

Copied from the phase 2 spec and the project's standing rules. Every task's requirements include these.

- **No new third-party dependencies. No build step.** stdlib plus the existing `requests` only.
- **No em dashes anywhere**, including UI copy, error sentences, comments, and docs.
- **No AI attribution anywhere**: no Co-Authored-By, no generated-with lines, in any commit or file.
- **Commit after every task, with explicit paths on `git add`, never `-A`** (a sweep once captured another agent's file mid-mutation).
- **Keys are never logged, echoed, or embedded.** This source needs no key; it must not invent one, and no URL with a query string ever enters a `TileFailure.reason` (compose from the fixed failure vocabulary, exactly as `sources/base.py` documents).
- **If a tile has no data then it has no data.** Nodata is NaN or None, never zero, never fabricated. A building whose footprint the LiDAR cannot answer for gets no height tag at all.
- **Fusion adds what is missing, never overwrites**: an existing `height` tag on a building is kept untouched.
- **Licence and attribution recorded per source**: licence "Open Government Licence v3.0", attribution "Contains Welsh Government and Natural Resources Wales information licensed under the Open Government Licence v3.0". Contour and height provenance name "Welsh Government LiDAR 2020 to 2023".
- **The resolution promise is 1 m.** Contour intervals finer than 1 m are generated and carry `"interpolated": true`; no copy anywhere claims 25 cm data.
- Live-network tests use the repo's existing `live` marker convention and are deselected by default; every other test runs offline from fixtures.
- Follow `ElevationSource`'s whole-area conventions for a source with no per-tile loop: `tile_failures` recorded against every handed tile, `tile_done`/`tile_skipped` with `tile_id="whole-area"`, reset of `tile_failures` at the top of every `fetch()`.
- Test evidence rules for this machine (paste into any mutation-verification brief): bump mtime before mutation runs (same-size same-second edits are invisible to the `.pyc` cache); the worktree is CRLF, so multi-line patterns written with `\n` match nothing; never revert a mutation with `git checkout --` over uncommitted intentional edits.

---

### Task 1: `bng.py` part 1, the GRS80 National Grid transverse Mercator

**Files:**
- Create: `src/mapgen/bng.py`
- Test: `tests/test_bng.py`

**Interfaces:**
- Consumes: nothing from the codebase (leaf module; deliberately does NOT touch `utm.py`, whose Urbano-matched arithmetic is pinned by phase 1 tests).
- Produces: `tm_forward(latitude: float, longitude: float) -> tuple[float, float]` (pseudo-BNG easting/northing of ETRS89 coordinates on GRS80), `tm_inverse(easting: float, northing: float) -> tuple[float, float]` (back to degrees), `class BngError(RuntimeError)`.

The projection constants, from OS's own guide (which ships inside the OSTN15 pack this project downloads in Task 2):

```python
# GRS80 ellipsoid, which is what OSTN15's input frame (ETRS89) is defined on.
_A = 6378137.0
_B = 6356752.314140356
# National Grid parameters: scale on the central meridian, true origin at
# 49 N 2 W, false origin 400 km west and 100 km north of it.
_F0 = 0.9996012717
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
_E0 = 400_000.0
_N0 = -100_000.0
```

- [ ] **Step 1: Write the failing tests**

```python
import math

import pytest

from mapgen.bng import tm_forward, tm_inverse


def test_tm_forward_true_origin_lands_on_false_origin_scaled():
    # The true origin projects to the false origin exactly, by construction.
    easting, northing = tm_forward(49.0, -2.0)
    assert easting == pytest.approx(400_000.0, abs=1e-6)
    assert northing == pytest.approx(-100_000.0, abs=1e-6)


def test_tm_roundtrip_over_wales():
    # Forward then inverse must return to the input to well under a
    # millimetre in degrees (1e-9 deg is about 0.1 mm on the ground).
    for lat, lon in [(51.40, -3.27), (51.48, -3.18), (53.32, -4.63), (52.42, -4.08)]:
        easting, northing = tm_forward(lat, lon)
        back_lat, back_lon = tm_inverse(easting, northing)
        assert back_lat == pytest.approx(lat, abs=1e-9)
        assert back_lon == pytest.approx(lon, abs=1e-9)


def test_tm_forward_monotonic_in_the_right_directions():
    # North increases northing, east increases easting: the cheapest way to
    # catch a swapped sign or a swapped argument order.
    e1, n1 = tm_forward(51.40, -3.27)
    e2, n2 = tm_forward(51.41, -3.27)
    e3, n3 = tm_forward(51.40, -3.26)
    assert n2 > n1 and abs(e2 - e1) < 200.0
    assert e3 > e1 and abs(n3 - n1) < 200.0
```

- [ ] **Step 2: Run the tests to verify they fail** (`pytest tests/test_bng.py -v`, expected: ImportError / module not found).

- [ ] **Step 3: Implement `tm_forward` and `tm_inverse`** using OS's published series (the guide's own term names, so the code can be read against the PDF):

```python
def _nu_rho_eta2(sin_lat: float) -> tuple[float, float, float]:
    e2 = (_A * _A - _B * _B) / (_A * _A)
    nu = _A * _F0 / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    rho = _A * _F0 * (1.0 - e2) / (1.0 - e2 * sin_lat * sin_lat) ** 1.5
    return nu, rho, nu / rho - 1.0


def _meridional_arc(lat: float) -> float:
    n = (_A - _B) / (_A + _B)
    n2, n3 = n * n, n * n * n
    d, s = lat - _LAT0, lat + _LAT0
    return _B * _F0 * (
        (1.0 + n + 1.25 * n2 + 1.25 * n3) * d
        - (3.0 * n + 3.0 * n2 + 2.625 * n3) * math.sin(d) * math.cos(s)
        + (1.875 * n2 + 1.875 * n3) * math.sin(2.0 * d) * math.cos(2.0 * s)
        - (35.0 / 24.0) * n3 * math.sin(3.0 * d) * math.cos(3.0 * s)
    )
```

then the I..VI terms for the forward and the iterated-M plus VII..XIIA terms for the inverse, exactly as the OS guide writes them (the inverse iterates `lat' += (N - N0 - M(lat')) / (a F0)` until `abs(N - N0 - M) < 1e-5` metres). Refuse non-finite inputs with a `BngError` sentence.

- [ ] **Step 4: Run the tests to verify they pass.**

- [ ] **Step 5: Commit** (`git add src/mapgen/bng.py tests/test_bng.py`).

Note for the reviewer: the roundtrip and origin tests pin internal consistency only; agreement with Ordnance Survey to survey accuracy is pinned in Task 2, where OS's own station test vectors run through `tm_forward` plus the real shift grid. That is deliberate: OS publishes no authoritative TM-only vectors for the GRS80 case, and the pack's worked example (user guide PDF) should be transcribed into a comment when the implementer has the pack in hand, not invented here from memory.

---

### Task 2: `bng.py` part 2, OSTN15 fetch, cache, parse, and the public transform

**Files:**
- Modify: `src/mapgen/bng.py`
- Create: `tests/fixtures/ostn15/` (station fixtures + grid slice + the helper that generated them)
- Test: `tests/test_bng.py`

**Interfaces:**
- Consumes: `tm_forward`, `tm_inverse` from Task 1.
- Produces:
  - `OSTN15_URL = "https://www.ordnancesurvey.co.uk/documents/resources/OSTN15-OSGM15-DevelopersPack.zip"`
  - `class Ostn15Grid` with `shift_at(easting: float, northing: float) -> tuple[float, float]` (bilinear over the 1 km nodes; raises `OutsideOstn15Error` when any corner node is absent or flagged outside the transformation)
  - `ensure_ostn15(cache_dir: Path | None = None, session=None) -> Ostn15Grid` (returns from cache; downloads the pack, parses the data file, writes the binary cache, on first use)
  - `load_ostn15(cache_dir: Path | None = None) -> Ostn15Grid | None` (cache only, never network)
  - `to_bng(latitude: float, longitude: float, grid: Ostn15Grid) -> tuple[float, float]`
  - `from_bng(easting: float, northing: float, grid: Ostn15Grid) -> tuple[float, float]` (iterates the shift, converging under 1e-4 m, max 10 rounds)
  - `class OutsideOstn15Error(BngError)`

**Mechanics to implement:**
- The data file is one record per 1 km node, 701 columns (E 0..700 km) by 1251 rows (N 0..1250 km). Verify the actual column layout against `OSTN15_OSGM15_TestFiles_README.txt` in the pack at implementation time rather than trusting anyone's recollection; the station tests below will catch a misread regardless. The horizontal shifts (east shift, north shift) are the only columns used; the geoid column is skipped because LiDAR heights are already Ordnance Datum Newlyn orthometric and mapgen never touches ellipsoidal heights.
- Binary cache at `<cache_dir>/ostn15_shifts.bin` (default cache_dir `~/.mapgen`, the directory `config.py` already owns): a 16-byte header (magic `b"OSTN15\x00\x01"`, then node count as `<Q`) followed by 701 x 1251 pairs of `<f` (east shift, north shift), row-major with the northing index outer, and NaN for a node the file does not cover. Parse once (about 41 MB of text), read the 7 MB cache forever after.
- `to_bng`: `(e, n) = tm_forward(lat, lon)`, then `(se, sn) = grid.shift_at(e, n)`, return `(e + se, n + sn)`.
- `from_bng`: start with `(e', n') = (E, N)`, iterate `(e', n') = (E - se, N - sn)` with shifts sampled at the current `(e', n')`, then `tm_inverse(e', n')`.
- The zip download goes through the provided `session` (a `requests.Session`) with a timeout, streams to a temp file, and extracts only `OSTN15_OSGM15_DataFile.txt` via `zipfile`. Read the licence notice PDF's name from the archive and record in the module docstring that the pack's own `OSGM15_Notice_of_release_for_developers.pdf` is the licence, quoting its operative sentence once the implementer has read it.

**Fixtures:** a committed helper script `tests/fixtures/ostn15/make_fixture.py` that, run by hand on a machine with the real pack, picks three stations from `OSTN15_OSGM15_TestInput_ETRStoOSGB.txt` (choose one Welsh or near-Wales station and two spread far apart), copies their input and expected output lines into `stations.txt`, and writes `grid_slice.bin` holding only the 3 x 3 blocks of 1 km nodes around each station's pseudo-BNG position (same binary format, with a small index header naming the blocks). The committed fixture is a few kilobytes; the 41 MB file is never committed.

- [ ] **Step 1: Write the failing tests**

```python
def test_station_vectors_match_os_expected_output(ostn15_fixture_grid):
    # The three OS Net stations from OS's own TestInput/TestOutput pair,
    # run through tm_forward plus the real shift values around them.
    for station in load_fixture_stations():
        easting, northing = to_bng(
            station.etrs_lat, station.etrs_lon, ostn15_fixture_grid
        )
        # OS publishes expected E/N to 3 decimal places.
        assert easting == pytest.approx(station.expected_e, abs=0.002)
        assert northing == pytest.approx(station.expected_n, abs=0.002)


def test_from_bng_inverts_to_bng(ostn15_fixture_grid):
    for station in load_fixture_stations():
        easting, northing = to_bng(
            station.etrs_lat, station.etrs_lon, ostn15_fixture_grid
        )
        lat, lon = from_bng(easting, northing, ostn15_fixture_grid)
        assert lat == pytest.approx(station.etrs_lat, abs=2e-9)
        assert lon == pytest.approx(station.etrs_lon, abs=2e-9)


def test_outside_grid_refuses(ostn15_fixture_grid):
    with pytest.raises(OutsideOstn15Error):
        ostn15_fixture_grid.shift_at(-50_000.0, -50_000.0)


def test_cache_roundtrip(tmp_path):
    # Parse a tiny synthetic data-file excerpt, write the cache, reload it,
    # and get identical shifts back.
    ...


def test_ensure_ostn15_never_downloads_when_cache_present(tmp_path):
    # A session whose get() raises AssertionError proves no network call.
    ...
```

plus one live test, marked with the repo's `live` marker, that calls `ensure_ostn15` against the real URL into a temp dir and runs EVERY station in the OS test files through `to_bng`, asserting all match to 2 mm.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement; generate the fixtures with the helper against the real pack (one manual download during implementation); commit the fixtures.**
- [ ] **Step 4: Run the offline suite green; run the live test once and record its pass in the task report.**
- [ ] **Step 5: Commit** (explicit paths: `src/mapgen/bng.py`, `tests/test_bng.py`, `tests/fixtures/ostn15/...`).

---

### Task 3: `cog.py`, windows out of a remote Cloud Optimized GeoTIFF

**Files:**
- Create: `src/mapgen/cog.py`
- Test: `tests/test_cog.py`

**Interfaces:**
- Consumes: `to_bng`/`from_bng` are NOT consumed here; `cog.py` works purely in BNG coordinates and stays datum-ignorant. (`BngWindow.sample(lat, lon, grid)` is the one convenience that takes an `Ostn15Grid` and delegates to `bng.to_bng` before `sample_bng`.)
- Produces:
  - `class CogError(ValueError)`
  - `class FileByteSource` (`path`; `read(start, length) -> bytes`, `size() -> int`, `name: str`)
  - `class HttpByteSource` (`url`, `session`, `timeout_seconds`; same protocol; sends `Range: bytes=start-end` with the project's User-Agent; a 200 answer to a range request, a missing Content-Range, or a short body is a `CogError`, because a server that ignores Range would otherwise stream 48 GB)
  - `class CogReader` with `CogReader.open(source) -> CogReader`, `.levels` (list of level descriptors: width, height, pixel_size, tile dims, tile counts, tag offsets), `.pixel_is_area: bool`, `.nodata: float | None`, `.epsg: int`, and `read_window(e_min, n_min, e_max, n_max, max_pixels=16_777_216) -> BngWindow`
  - `class BngWindow` (dataclass): `e_origin`, `n_top`, `pixel_size`, `width`, `height`, `values: array("f")` with NaN already substituted for nodata; `sample_bng(easting, northing) -> float | None` (bilinear between pixel centres, nodata corners at zero weight, None when all four are empty or the point is outside); `sample(latitude, longitude, grid) -> float | None`; `bounds() -> tuple[float, float, float, float]`

**Mechanics:**
- Parse classic AND BigTIFF headers (the mosaics are BigTIFF; the Task 5 writer emits classic, and one reader serves both). Walk the whole IFD chain; refuse compression other than 1/8/32946, predictor other than 1, sample format/bits other than float32 and int16, and a missing tiepoint or pixel scale, each with a sentence naming the file and the offending value, in `geotiff.py`'s error style.
- GeoKeys: require a projected model (type 1) and record the EPSG from key 3072. `read_window` refuses a source whose EPSG is not 27700 (the caller chose the wrong file; say so). Honour PixelIsArea AND PixelIsPoint: centre of pixel (col, row) is origin plus `(col + 0.5) * scale` for area, `col * scale` for point.
- Level selection inside `read_window`: walk levels from finest to coarsest, take the first whose window pixel count fits `max_pixels`. Record the level's pixel size on the returned window (that is the honest `source_resolution` downstream copy quotes).
- Tile fetch: compute the tile range covering the window, range-read only the needed slices of TileOffsets/TileByteCounts (8 and 4 bytes per tile at the entry's recorded array offset), coalesce adjacent tiles whose file ranges are contiguous into single requests, decompress with `zlib`, and copy the window's intersection with each tile into `values`. Tiles wholly outside the raster's tile grid contribute nodata.
- One retry per failed range request, then raise `CogError` carrying the transport phrase from `classify_transport_failure` (import it from `sources.base`; do not invent a second classifier).

- [ ] **Step 1: Write the failing tests.** The test file builds synthetic COGs in memory with a small local helper (`_make_cog(...)` writing classic TIFF bytes with chosen dims, tiepoint, PixelIsArea/Point, nodata, deflate tiles), served to `CogReader` through `FileByteSource` and through a fake byte source that counts and records range requests. Cases:

```python
def test_reads_window_values_exactly_where_they_were_written(tmp_path): ...
def test_pixel_is_area_centres_offset_by_half_a_pixel(tmp_path): ...
def test_nodata_pixels_become_nan_and_sample_returns_none(tmp_path): ...
def test_bilinear_matches_hand_computed_value(tmp_path): ...
def test_level_selection_prefers_finest_that_fits_max_pixels(tmp_path): ...
def test_only_needed_tile_index_slices_are_fetched(counting_source): ...
def test_adjacent_tiles_coalesce_into_one_range_request(counting_source): ...
def test_refuses_wrong_epsg_lzw_and_predictor(tmp_path): ...
def test_range_ignoring_server_is_refused(fake_200_source): ...
```

plus one `live` test: open the real DTM URL, read a 500 x 500 m window over Barry Island (BNG roughly E 311_000..311_500, N 166_400..166_900), assert the level chosen is full resolution, that heights on land are finite and between -5 and +150 m, and that a window in the Bristol Channel (E 320_000..320_500, N 155_000..155_500) comes back entirely None-sampling.

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Offline suite green; run the live test once, record timing and byte counts in the task report (these numbers feed Task 6's estimate constants).**
- [ ] **Step 5: Commit** (`git add src/mapgen/cog.py tests/test_cog.py`).

---

### Task 4: `contours.py`, marching squares to GeoJSON

**Files:**
- Create: `src/mapgen/contours.py`
- Test: `tests/test_contours.py`

**Interfaces:**
- Consumes: `BngWindow` (Task 3), `from_bng` + `Ostn15Grid` (Task 2).
- Produces:
  - `contour_intervals_for(window: BngWindow) -> list[float]`, the policy: `5.0` always; `1.0` when the window covers at most 6.0 square kilometres; `0.5` and `0.25` when at most 1.5 square kilometres.
  - `generate_contours(window, interval: float) -> list[list[tuple[float, float]]]`, polylines in BNG metres.
  - `write_contour_files(window, out_dir: Path, stem: str, grid: Ostn15Grid) -> list[Path]`, one GeoJSON FeatureCollection per generated interval, named `<stem>_contours_5m.geojson`, `<stem>_contours_1m.geojson`, `<stem>_contours_0.5m.geojson`, `<stem>_contours_0.25m.geojson` (only the intervals the policy grants this window).

**Mechanics:**
- For the 5 m interval, decimate the grid first: stride `max(1, floor(4.0 / window.pixel_size))` so context contours come off roughly 4 m data rather than 16 million cells.
- Marching squares on pixel-centre corners: per cell, compute min and max of the four corners once, loop only over the contour levels crossing that cell (this is what keeps 0.25 m intervals affordable); skip any cell with a NaN corner; resolve the two saddle cases by the cell-centre average, the standard disambiguation.
- Join segments into polylines by hashing endpoints rounded to 1e-6 m; emit closed rings closed (first point repeated last). Drop vertices collinear with their neighbours to within 0.05 m perpendicular distance; no other simplification.
- GeoJSON coordinates are `[longitude, latitude]` via `from_bng` per vertex. Feature properties, exactly these keys: `elevation` (float, the level), `interval_m` (float), `source` (`"Welsh Government LiDAR 2020 to 2023"`), `source_resolution_m` (float, `window.pixel_size`), `interpolated` (bool, true exactly when `interval_m < 1.0`). No em dashes in any string.

- [ ] **Step 1: Write the failing tests**, on tiny synthetic windows where the answer is checkable by hand:

```python
def test_single_cone_produces_concentric_closed_rings(): ...
def test_planar_ramp_produces_straight_parallel_lines_at_exact_levels(): ...
def test_nan_region_breaks_lines_rather_than_bridging_it(): ...
def test_saddle_does_not_self_cross(): ...
def test_interval_policy_thresholds():
    # 2.0 km x 2.0 km window: [5.0, 1.0]. 1.0 x 1.0 km: all four.
    # 4.0 x 4.0 km: [5.0] only.
    ...
def test_geojson_properties_and_interpolated_flag(): ...
def test_vertices_are_wgs84_via_from_bng(ostn15_fixture_grid): ...
```

- [ ] **Step 2: Run to verify failure.** **Step 3: Implement.** **Step 4: Green.**
- [ ] **Step 5: Commit** (`git add src/mapgen/contours.py tests/test_contours.py`).

---

### Task 5: `geotiff_write.py`, a raster writer `cog.py` can read back

**Files:**
- Create: `src/mapgen/geotiff_write.py`
- Test: `tests/test_geotiff_write.py`

**Interfaces:**
- Consumes: `BngWindow`, `CogReader`, `FileByteSource` (Task 3), `atomic_write_bytes` from `mapgen.fsutil`.
- Produces: `write_bng_geotiff(path: Path, window: BngWindow) -> None`. Classic little-endian TIFF, tiled 256 x 256, deflate, float32, GDAL_NODATA `-9999` (NaN values written as -9999.0), EPSG:27700 geokeys with PixelIsArea, ModelPixelScale from `window.pixel_size`, ModelTiepoint placing pixel (0,0)'s outer corner at `(window.e_origin, window.n_top)`. Written atomically.

Why this exists: the packaged `<stem>_lidar_dtm.tif` and `<stem>_lidar_dsm.tif` are what let `mapgen bridge` rebuild the `.egrid` and re-fuse heights on a finished package after work_dir is gone, and they are 1 m rasters the owner can sample in Grasshopper directly. The reader check is `cog.py` itself: one parser, both directions, no second TIFF dialect to drift.

- [ ] **Step 1: Failing tests**: write a window, read it back through `CogReader.open(FileByteSource(path))`, assert bit-identical values (NaN positions included), identical georeference, correct EPSG and PixelIsArea; a window larger than one tile exercises multi-tile layout; refuse a window with zero dimensions with a sentence.
- [ ] **Step 2: Verify failure.** **Step 3: Implement.** **Step 4: Green.**
- [ ] **Step 5: Commit** (`git add src/mapgen/geotiff_write.py tests/test_geotiff_write.py`).

---

### Task 6: `sources/lidar_wales.py`, the LayerSource

**Files:**
- Create: `src/mapgen/sources/lidar_wales.py`
- Modify: `src/mapgen/package.py` (`register_default_sources` gains `LidarWalesSource()`), `check_path_length` inputs if its arithmetic needs the new filenames (implementer verifies)
- Test: `tests/test_lidar_wales.py`

**Interfaces:**
- Consumes: Tasks 2, 3, 4, 5; `Estimate`, `TileFailure`, `ProgressSink`, failure vocabulary and classifiers from `sources/base.py`; `CancelToken`.
- Produces the registered source:

```python
class LidarWalesSource:
    id = "lidar_wales"
    display_name = "LiDAR terrain (Wales, 1 m)"
    licence = "Open Government Licence v3.0"
    attribution = (
        "Contains Welsh Government and Natural Resources Wales information "
        "licensed under the Open Government Licence v3.0"
    )
    requires_api_key = False
    DTM_URL = ".../wales_dtm_32bit_cog.tif"   # the probed URL, verbatim
    DSM_URL = ".../wales_dsm_32bit_cog.tif"
    # The mosaic's own bounds, from its header: E 164993..356000, N 164000..397000.
    MOSAIC_BOUNDS = (164_993.0, 164_000.0, 356_000.0, 397_000.0)
```

**Behaviour:**
- `estimate(bbox, tiles)`: window pixels for the padded extent (reuse `PAD_METRES` from `egrid.py` so the window always covers the grid the egrid step will sample) times two rasters times provisional `BYTES_PER_WINDOW_PIXEL = 2.6` (4 bytes deflated; refit from Task 3's live measurement with a comment in `elevation.py`'s measured-constant style), seconds from the measured range-request timings with a floor. No network in `estimate`.
- `fetch(bbox, tiles, work_dir, progress, cancel=None)`: reset `tile_failures`; check `cancel`; skip when both work files exist non-empty (`tile_skipped`, `tile_id="whole-area"`); `ensure_ostn15` first (its download failure is classified and recorded like any transport failure); project the padded bbox corners to BNG; refuse an extent wholly outside `MOSAIC_BOUNDS` by recording `FAILURE_NO_OUTPUT` against every tile with the sentence `"The Welsh LiDAR mosaic has no data for this extent. It covers Wales only."` and raising; otherwise read the DTM window then the DSM window (cancel checkpoint between them), refuse an all-nodata pair with the sentence `"The Welsh LiDAR mosaic is empty over this extent, which usually means open water."` and `FAILURE_NO_OUTPUT`; write `lidar_dtm.tif` and `lidar_dsm.tif` into work_dir via `write_bng_geotiff`; `tile_done` with `tile_id="whole-area"`. Transport and status failures go through the shared classifiers into `tile_failures` against every handed tile, sentences from the fixed vocabulary, never a URL.
- `merge(parts, out_dir, stem)`: pick its own two work files by name from `parts` (the `ElevationSource.merge` lesson: never `parts[0]`), copy to `<stem>_lidar_dtm.tif` and `<stem>_lidar_dsm.tif`, generate contour files from the DTM window via `write_contour_files`, return every written path. Missing work files mean returning only what exists, and stale package copies of the missing ones are unlinked, mirroring `ElevationSource.merge`'s I8 discipline.
- `possible_outputs(stem)`: all six names (`_lidar_dtm.tif`, `_lidar_dsm.tif`, four contour files).
- Windows come back with their level's pixel size; when the padded extent forces an overview level, every downstream `source_resolution_m` copies it honestly and nothing anywhere claims 1 m.

- [ ] **Step 1: Failing tests** against fake byte sources built from Task 3's synthetic-COG helper (no network): estimate arithmetic; fetch happy path writes both work files; skip-on-resume; outside-Wales refusal (kind, sentence, raised); all-nodata refusal; cancel between rasters leaves no failure records; merge naming, contour files appear, stale-copy unlink; `possible_outputs` closed list; registry: `register_default_sources()` registers it once, `type` check honoured on repeat.
- [ ] **Step 2: Verify failure.** **Step 3: Implement.** **Step 4: Green, plus one `live` test: full fetch+merge over a 400 x 400 m Barry extent into a temp dir, asserting the six outputs and finite contour elevations.**
- [ ] **Step 5: Commit** (`git add src/mapgen/sources/lidar_wales.py src/mapgen/package.py tests/test_lidar_wales.py`).

---

### Task 7: `heights.py` and the fusion post-step

**Files:**
- Create: `src/mapgen/heights.py`
- Modify: `src/mapgen/package.py` (post-step in `run_survey` and `bridge_package`, survey.json block)
- Test: `tests/test_heights.py`, additions to `tests/test_api.py`-adjacent package tests where the existing suites cover survey.json shape

**Interfaces:**
- Consumes: `CogReader`/`FileByteSource`/`BngWindow` (packaged tifs read back), `to_bng`, `load_ostn15`/`ensure_ostn15`.
- Produces:
  - `fuse_building_heights(osm_path: Path, dtm: BngWindow, dsm: BngWindow, grid: Ostn15Grid) -> HeightsRecord`
  - `@dataclass(frozen=True) class HeightsRecord: buildings: int; written: int; kept_existing: int; no_data: int; relations_skipped: int`
  - In `package.py`: `_fuse_heights_step(root, stem, sink) -> dict` returning a five-keys-always record `{"written": int|None, "buildings": int|None, "kept_existing": int|None, "no_data": int|None, "error": str|None}` stored in survey.json as `"lidar_heights"`, following `_elevation_grid_record`'s one-shape-whatever-happened rule. The step runs when `<stem>.osm`, `<stem>_lidar_dtm.tif` and `<stem>_lidar_dsm.tif` all exist; otherwise records the all-None shape. It runs BEFORE `_run_bridge_step` (the bridge converts the `.osm`, so heights must already be in it) in `run_survey` and in `bridge_package`.

**Fusion rules (the owner's rulings, encoded):**
- Ways tagged `building=*` only; multipolygon relations are counted in `relations_skipped`, untouched.
- A way that already has a `height` tag is kept untouched (`kept_existing`).
- Footprint nodes to BNG; interior sample points on a 1 m grid within the footprint's bounding box, filtered by even-odd point-in-polygon; require at least 3 sample points where BOTH rasters answer, else `no_data` and no tag (nothing is fabricated).
- `height = p90(dsm samples) - median(dtm samples)`, rounded to one decimal; heights below 2.0 m are recorded as `no_data` rather than tagged (a slab reading is noise, not a building height).
- Written tags: `height="<value>"` and `source:height="Welsh Government LiDAR 2020 to 2023 (DSM minus DTM)"`. The `.osm` is rewritten atomically with `xml.etree`, preserving everything else byte-for-what-matters (same encoding declaration, same structure).
- Idempotent by construction: a second run finds the tags present and keeps them.

- [ ] **Step 1: Failing tests**: a hand-built tiny `.osm` (three buildings: one flat-roofed box over a synthetic DSM/DTM pair with a known 6.0 m difference, one already tagged `height=9`, one out on nodata) plus synthetic windows; assert tag values, `kept_existing`, `no_data`, relation skip, atomicity (temp file never left), idempotence, and the survey.json block shape in a package-level test through `run_survey` with stub sources.
- [ ] **Step 2: Verify failure.** **Step 3: Implement.** **Step 4: Green.**
- [ ] **Step 5: Commit** (`git add src/mapgen/heights.py src/mapgen/package.py tests/test_heights.py` plus the touched package tests).

---

### Task 8: the egrid upgrade

**Files:**
- Modify: `src/mapgen/egrid.py`, `src/mapgen/package.py`
- Test: additions to `tests/test_egrid.py` and the package-level suites

**Interfaces:**
- `egrid.py` produces `write_elevation_grid_from_sampler(sampler, bbox, zone, target) -> ElevationGrid` where `sampler` is anything with `sample(latitude, longitude) -> float | None`; the existing `write_elevation_grid(tiff, ...)` becomes a thin wrapper that builds a `DemRaster` and delegates. `build_grid`'s loop is untouched (it already only calls `.sample`).
- A small `class _ChainSampler` in `package.py` (or `egrid.py`; implementer's judgment, one home only): `sample()` asks the LiDAR DTM first (via `BngWindow.sample(lat, lon, grid)`), falls back to the OpenTopography `DemRaster` when the answer is None. The vertical datum mismatch between ODN and COP30's EGM surface at the seam is accepted and documented in one comment: the fallback fires over sea and beyond the mosaic edge, where COP30's filled water plane is exactly the value phase 1 shipped.
- `_write_elevation_grid_step` decides: LiDAR DTM present (and OSTN15 loadable, `load_ostn15` then `ensure_ostn15` as fallback) means chain (or LiDAR alone when no `<stem>.tif`); else the phase 1 path unchanged; neither raster present means the existing removal branch. The `elevation_grid` record gains a sixth always-present key `"source"`: one of `"lidar_wales+opentopography"`, `"lidar_wales"`, `"opentopography"`, `None`. README's schema table is updated in the same commit (the record's docstring says why keys are always all present; keep that promise).

- [ ] **Step 1: Failing tests**: chain sampler precedence (LiDAR answers, fallback fires on None, None+None stays None); `write_elevation_grid_from_sampler` equals the old path byte-for-byte when fed the same DemRaster; package-level: a package with lidar tifs gets `source="lidar_wales+opentopography"` and a grid whose covered count strictly exceeds the COP30-only grid on a fixture where LiDAR covers more nodes; bridge on a package with lidar tifs rebuilds the egrid the same way; the record always carries all six keys on every branch.
- [ ] **Step 2: Verify failure.** **Step 3: Implement.** **Step 4: Green.**
- [ ] **Step 5: Commit** (`git add src/mapgen/egrid.py src/mapgen/package.py tests/test_egrid.py` plus touched suites and README).

---

### Task 9: surface, copy, docs, and the end-to-end proof

**Files:**
- Modify: `src/mapgen/web/static/app.js` / `index.html` only if the registry-driven checklist does NOT already surface the new source (verify first; phase 1 built the layer checklist and settings from `/api/sources`, so the expected diff here is zero); `README.md` (package contents table, survey.json schema); `docs/superpowers/HANDOFF.md`
- Test: Node suite additions only if app.js changed

- [ ] **Step 1: Verify the browser checklist shows "LiDAR terrain (Wales, 1 m)" with no key field, purely from the registry; screenshot-level confirmation in the report. Fix only what is actually missing.**
- [ ] **Step 2: Refit the estimate constants** from Task 3's and Task 6's live measurements, recorded in the measured-constant comment style `elevation.py` uses (values, dates, extents, and how thin the evidence is).
- [ ] **Step 3: One live end-to-end run** over a small Barry extent with all four sources selected: assert the package holds the `.osm` (with at least one fused `height` tag), the two lidar tifs, at least `contours_5m` and `contours_1m`, the `.egrid` with `source="lidar_wales+opentopography"`, and a survey.json whose provenance block for `lidar_wales` carries the OGL licence and attribution sentences. Record byte sizes and wall time in the report.
- [ ] **Step 4: Update README's package-contents and survey.json schema sections; update HANDOFF's "Then" section to name item 2 (INSPIRE curves) as next.**
- [ ] **Step 5: Commit** (explicit paths).

---

## Decisions made by this plan (so nobody relitigates them mid-task)

1. **Full OSTN15, not the Lite pack, not Helmert.** 0.1 m against 1 m data, one 15.5 MB download cached forever, and it is the georeference backbone for every later EPSG:27700 source. The Lite pack exists (probed, 1.26 MB) if the full file ever vanishes.
2. **The 32 bit mosaics, full-resolution-first with automatic overview fallback** above 16,777,216 window pixels (the same ceiling `geotiff.py` already uses), recording the honest pixel size everywhere downstream.
3. **Packaged rasters are first-class outputs** (`_lidar_dtm.tif`, `_lidar_dsm.tif`) so `mapgen bridge` can rebuild the egrid and re-fuse heights on finished packages, and so Grasshopper gets the 1 m surfaces directly.
4. **Contours ship per interval file**, WGS84 GeoJSON, `interpolated: true` below 1 m, generated rather than downloaded, from the same window the package already paid to fetch.
5. **Heights fuse only where missing, only from at least 3 valid samples, minimum 2.0 m**, `p90(DSM) - median(DTM)`, with `source:height` naming the data. Relations are skipped and counted this round.
6. **Coverage refusal is fetch-time** with a plain sentence; estimate-time coverage intelligence is item 3's resolver, not this task's invention.
7. **`utm.py` and `geotiff.py` are untouched.** Their Urbano-matched behaviour is pinned; the new formats live in new modules with one shared reader (`cog.py`) for both directions of the new TIFF traffic.

## Self-review notes

- Spec coverage: contours per scale with GH-friendly properties (Task 4), DSM-DTM heights (Task 7), egrid upgrade (Task 8), OGL attribution recorded (Tasks 6, 9), 1 m honesty and the interpolated label (Global Constraints, Tasks 4, 6), fusion-adds-never-overwrites (Task 7). Replacement machinery: correctly absent (deferred by task zero).
- Type consistency: `Ostn15Grid` flows Task 2 into Tasks 3 (convenience sample), 4 (vertex unprojection), 7 (footprint projection), 8 (chain sampler); `BngWindow` flows Task 3 into 4, 5, 6, 7, 8. Signatures named identically throughout.
- No placeholder step describes intent without mechanics; where a value must come from the live source at implementation time (OSTN15 column order, the user guide's worked example), the plan says exactly which file settles it and which test catches a misread.
