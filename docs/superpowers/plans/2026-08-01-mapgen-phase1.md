# mapgen Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the working `osm_overture_tiles.py` CLI into `mapgen`, an operable site survey tool with a local web UI, date and location based output naming, resume after failure, and a clean seam for phase 2 data sources.

**Architecture:** The existing 1,523-line module is split into focused modules under `src/mapgen/`, moving functions rather than rewriting them. A `LayerSource` protocol abstracts each data source so `package.py` orchestrates without knowing about any specific one. A stdlib HTTP server on loopback serves a Leaflet map picker and streams job progress over Server-Sent Events. The C# `UrbanoBridge` gains one argument so the Urbano file stem can be human readable.

**Tech Stack:** Python 3.11+, `requests`, `overturemaps`, pytest, stdlib `http.server`, vendored Leaflet 1.9.4, .NET 10 for the existing bridge.

**Spec:** `docs/superpowers/specs/2026-08-01-mapgen-design.md`

## Global Constraints

- Python `requires-python = ">=3.11"`. Target interpreter on this machine is 3.13.13.
- Never add `Co-Authored-By` or any AI attribution to commits. This is a hard rule.
- No em dashes in any prose, comments, docstrings, or UI copy.
- Slug rules, verbatim from spec: transliterate to ASCII, spaces to hyphens, drop characters outside `[A-Za-z0-9-]`, collapse repeated hyphens, trim leading and trailing hyphens, cap each component at 40 characters. Empty result after slugification is rejected with a message naming the offending field.
- Path length guard threshold: **240 characters**, checked before any network call.
- Folder scheme: `<output root>/<Region>/<YYYY-MM-DD>_<Site>/`. File stem: `<Site>_<YYYY-MM-DD>`.
- Collisions append `_02`, `_03` to the dated folder name.
- `survey.json` `schema_version` is `1`.
- All file writes are write-to-temp-then-rename. No exceptions. This includes output written by a third-party process on our behalf: point it at a `.part` path and rename once it exits successfully.
- Tests never make live network calls. The one live smoke test is marked `@pytest.mark.live` and excluded from the default run.
- The legacy subcommand names `plan`, `download`, `merge` and `urbano-package` remain available as aliases for `survey` and `estimate`, carrying the **new** flag set. They are not flag-compatible with the old script: `--region` and `--site` are required, and `--output-dir`, `--tile-id` and `--max-tiles` are gone. An old invocation fails with a clear argument error rather than doing something subtly different.

## Deviations from the spec

Two, both deliberate, neither changing what the tool does.

**1. An extra module.** The spec's module list omits a home for the shared atomic-write helper, which `merge.py`, `sources/*.py` and `package.py` all need. This plan adds `src/mapgen/fsutil.py` for filesystem primitives (atomic write, ensure dir, best-effort rmtree, the work directory scope). One responsibility, no logic beyond filesystem mechanics.

**2. Progress transport is polling, not Server-Sent Events.** The spec specifies `GET /api/jobs/<id>/events` as an SSE stream. This plan implements `GET /api/jobs/<id>`, returning the accumulated event list, which the browser polls every 700 ms. SSE over `BaseHTTPRequestHandler` holds a worker thread open for the life of the job and needs careful handling of client disconnects, chunked encoding and keep-alive, all to deliver sub-second updates to a loopback tab watching a job measured in minutes. Polling is a fraction of the code, cannot leak threads, and recovers from a closed tab for free. Every other endpoint in the spec's API surface is implemented as written. If live streaming is wanted later, the `EventLog` listener hook in `jobs.py` is the seam to add it at.

---

### Task 1: Repository scaffolding and test harness

**Files:**
- Create: `pyproject.toml`
- Create: `bootstrap.ps1`
- Create: `src/mapgen/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_version.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `mapgen.__version__` (str), an installed `mapgen` console script pointing at `mapgen.cli:main`, and a working `pytest` invocation. Every later task depends on this layout.

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` as an empty file, then create `tests/test_version.py`:

```python
import re

import mapgen


def test_version_is_a_semver_string():
    assert isinstance(mapgen.__version__, str)
    assert re.fullmatch(r"\d+\.\d+\.\d+", mapgen.__version__)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_version.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen'`

- [ ] **Step 3: Create the package and project metadata**

Create `src/mapgen/__init__.py`:

```python
"""mapgen: site survey data packaging for architectural work."""

__version__ = "1.0.0"
```

Create `pyproject.toml`:

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "mapgen"
version = "1.0.0"
description = "Site survey data packaging for architectural work"
requires-python = ">=3.11"
dependencies = [
    "requests>=2.32,<3",
    "overturemaps>=0.10,<1",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
]

[project.scripts]
mapgen = "mapgen.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
mapgen = ["web/static/*", "web/static/**/*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
markers = [
    "live: hits the real network, excluded from the default run",
]
addopts = "-m 'not live'"
```

- [ ] **Step 4: Create the bootstrap script**

Create `bootstrap.ps1`:

```powershell
# Creates .venv and installs mapgen in editable mode with dev extras.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

if (-not (Test-Path "$root\.venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv "$root\.venv"
}

$python = "$root\.venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -e ".[dev]"

Write-Host ""
Write-Host "Done. Activate with:"
Write-Host "  .\.venv\Scripts\Activate.ps1"
Write-Host "Then run:"
Write-Host "  mapgen ui"
```

- [ ] **Step 5: Bootstrap the environment**

Run: `.\bootstrap.ps1`
Expected: `.venv` created, `mapgen` installed in editable mode. If `overturemaps` fails to resolve at the pinned floor, record the actual resolved version and widen the floor in `pyproject.toml` to match, then rerun.

- [ ] **Step 6: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_version.py -v`
Expected: PASS

- [ ] **Step 7: Record resolved dependency versions**

Run: `.\.venv\Scripts\python.exe -m pip freeze | Select-String "requests|overturemaps|pytest"`
Note the output in the commit body so future environments can be reproduced.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml bootstrap.ps1 src/mapgen/__init__.py tests/__init__.py tests/test_version.py
git commit -m "build: package skeleton, pinned deps, pytest harness"
```

---

### Task 2: Bridge accepts a readable file stem, and Urbano verifies it

This task is deliberately second. The entire naming scheme rests on Urbano 2 accepting a renamed stem, and that cannot be settled by an automated test. Finding out now costs an hour. Finding out at Task 12 costs the plan.

**Files:**
- Modify: `tools/UrbanoBridge/Program.cs:55` (the `fileNameStr` assignment)
- Modify: `tools/UrbanoBridge/Program.cs:1046-1146` (the `Options` class)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the bridge accepts `--file-name-stem <string>`. When omitted, behaviour is byte-identical to today. Task 11 (`bridge.py`) passes this argument.

- [ ] **Step 1: Add the property to the Options class**

In `tools/UrbanoBridge/Program.cs`, inside `sealed class Options`, add after the `TravelerModelPath` property (currently line 1054):

```csharp
    public string? FileNameStem { get; init; }
```

- [ ] **Step 2: Add the local variable in Options.Parse**

After `string? travelerModelPath = null;` (currently line 1069), add:

```csharp
        string? fileNameStem = null;
```

- [ ] **Step 3: Add the argument case**

In the `switch (argument)` block, after the `case "--traveler-model":` block (currently lines 1099-1101), add:

```csharp
                case "--file-name-stem":
                    fileNameStem = NextValue(args, ref index, argument);
                    break;
```

- [ ] **Step 4: Pass it into the constructed Options**

In the `return new Options { ... }` block, after `TravelerModelPath = travelerModelPath,` (currently line 1140), add:

```csharp
            FileNameStem = fileNameStem,
```

- [ ] **Step 5: Use it, falling back to the coordinate form**

Replace line 55:

```csharp
    var fileNameStr = BuildFileNameString(options.BBox);
```

with:

```csharp
    var fileNameStr = string.IsNullOrWhiteSpace(options.FileNameStem)
        ? BuildFileNameString(options.BBox)
        : options.FileNameStem;
```

`BuildFileNameString` stays in place as the fallback. Do not delete it.

- [ ] **Step 6: Build the bridge**

Run: `dotnet build tools/UrbanoBridge/UrbanoBridge.csproj`
Expected: build succeeds with no errors.

- [ ] **Step 7: Verify the default path is unchanged**

Run the bridge with no `--file-name-stem` on a tiny bbox, into a scratch folder.

Note the space between `--bbox` and its value. The bridge's hand-rolled parser at `Options.Parse` matches on the exact token `--bbox`, so the `--bbox=value` form argparse accepts is rejected here as an unknown argument.

```powershell
dotnet run --project tools/UrbanoBridge/UrbanoBridge.csproj -- `
  --bbox -3.29,51.38,-3.28,51.39 `
  --output-folder .\_verify\coord `
  --skip-blocks --skip-climate --skip-elevation
```

Expected: console prints `File name stem: 51.39_51.38_-3.28_-3.29` and writes `51.39_51.38_-3.28_-3.29_project_setting.json`. This proves the fallback is intact.

- [ ] **Step 8: Verify the readable stem is honoured**

```powershell
dotnet run --project tools/UrbanoBridge/UrbanoBridge.csproj -- `
  --bbox -3.29,51.38,-3.28,51.39 `
  --output-folder .\_verify\named `
  --file-name-stem "Barry-Waterfront_2026-08-01" `
  --skip-blocks --skip-climate --skip-elevation
```

Expected: console prints `File name stem: Barry-Waterfront_2026-08-01` and writes `Barry-Waterfront_2026-08-01_project_setting.json`. Open that JSON and confirm `FileNameStr` is the readable stem while `Top`, `Bottom`, `Left`, `Right` still carry the real coordinates.

- [ ] **Step 9: MANUAL GATE, load the renamed package in Urbano 2**

Open Rhino and Grasshopper, place the Urbano 2 component that consumes a project setting file, and point it at `_verify\named\Barry-Waterfront_2026-08-01_project_setting.json`.

**If it loads:** the naming scheme is confirmed. Record the Urbano version tested in the commit body and continue to Task 3.

**If it rejects the file:** stop and report. The fallback is spec-approved: keep the readable folder name and revert the stem to the coordinate form. Only the naming section of the spec changes, and Task 4 is amended to always use `BuildFileNameString` output as the stem. Do not proceed to Task 4 without resolving this.

- [ ] **Step 10: Probe the Layers array (upside only, not a gate)**

Edit `_verify\named\Barry-Waterfront_2026-08-01_project_setting.json` by hand, setting `"Layers"` to a list containing one absolute path to any GeoJSON file on disk. Reload in Urbano 2 and observe whether anything changes.

Record the outcome in the commit body as either `Layers: consumed` or `Layers: ignored`. Task 12 reads this note to decide whether to populate the array. Either outcome is acceptable and neither blocks progress.

- [ ] **Step 11: Clean up and commit**

```bash
rm -rf _verify
git add tools/UrbanoBridge/Program.cs
git commit -m "feat(bridge): accept --file-name-stem, falling back to the coordinate form"
```

---

### Task 3: geo.py, bbox and tiling

**Files:**
- Create: `src/mapgen/geo.py`
- Test: `tests/test_geo.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `BBox` frozen dataclass with fields `west, south, east, north: float`, classmethod `parse(value: str) -> BBox`, methods `as_tuple() -> tuple[float, float, float, float]`, `to_dict() -> dict[str, float]`, `to_query_string() -> str`, property `centre -> tuple[float, float]` returning `(lon, lat)`.
  - `Tile` frozen dataclass with fields `tile_id: str`, `row: int`, `col: int`, `core_bbox: BBox`, `query_bbox: BBox`, method `to_dict() -> dict[str, object]`.
  - `build_tiles(bbox: BBox, tile_size_m: float, overlap_m: float) -> list[Tile]`
  - `extent_metres(bbox: BBox) -> tuple[float, float]` returning `(width_m, height_m)`.
  - `BBoxError(ValueError)` raised by `BBox.parse` and validation.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_geo.py`:

```python
import pytest

from mapgen.geo import BBox, BBoxError, build_tiles, extent_metres


def test_parse_accepts_west_south_east_north():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    assert bbox.west == pytest.approx(-3.6626)
    assert bbox.south == pytest.approx(51.3709)
    assert bbox.east == pytest.approx(-3.1483)
    assert bbox.north == pytest.approx(51.5476)


def test_parse_normalises_reversed_pairs():
    bbox = BBox.parse("-3.1483,51.5476,-3.6626,51.3709")
    assert bbox.west == pytest.approx(-3.6626)
    assert bbox.south == pytest.approx(51.3709)
    assert bbox.east == pytest.approx(-3.1483)
    assert bbox.north == pytest.approx(51.5476)


def test_parse_rejects_wrong_field_count():
    with pytest.raises(BBoxError, match="4 comma-separated"):
        BBox.parse("-3.66,51.37,-3.14")


def test_parse_rejects_non_numeric():
    with pytest.raises(BBoxError, match="Invalid bbox"):
        BBox.parse("-3.66,51.37,-3.14,north")


def test_parse_rejects_out_of_range_longitude():
    with pytest.raises(BBoxError, match="Longitude"):
        BBox.parse("-200,51.37,-3.14,51.54")


def test_parse_rejects_out_of_range_latitude():
    with pytest.raises(BBoxError, match="Latitude"):
        BBox.parse("-3.66,-91,-3.14,51.54")


def test_parse_rejects_zero_area():
    with pytest.raises(BBoxError, match="zero width or height"):
        BBox.parse("-3.66,51.37,-3.66,51.54")


def test_query_string_is_seven_decimal_places():
    bbox = BBox.parse("-3.5,51.5,-3.4,51.6")
    assert bbox.to_query_string() == "-3.5000000,51.5000000,-3.4000000,51.6000000"


def test_centre_is_the_midpoint():
    lon, lat = BBox.parse("-4,51,-2,53").centre
    assert lon == pytest.approx(-3.0)
    assert lat == pytest.approx(52.0)


def test_extent_metres_is_plausible_for_a_known_box():
    width_m, height_m = extent_metres(BBox.parse("-3.6626,51.3709,-3.1483,51.5476"))
    assert width_m == pytest.approx(35_600, rel=0.02)
    assert height_m == pytest.approx(19_700, rel=0.02)


def test_build_tiles_covers_the_area_in_a_grid():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    tiles = build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0)
    assert len(tiles) == 32
    assert tiles[0].tile_id == "r00_c00"
    assert tiles[0].row == 0 and tiles[0].col == 0
    assert tiles[-1].tile_id == "r03_c07"


def test_build_tiles_query_bbox_is_never_smaller_than_core():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    for tile in build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0):
        assert tile.query_bbox.west <= tile.core_bbox.west
        assert tile.query_bbox.south <= tile.core_bbox.south
        assert tile.query_bbox.east >= tile.core_bbox.east
        assert tile.query_bbox.north >= tile.core_bbox.north


def test_build_tiles_query_bbox_is_clamped_to_the_study_area():
    bbox = BBox.parse("-3.6626,51.3709,-3.1483,51.5476")
    for tile in build_tiles(bbox, tile_size_m=5000.0, overlap_m=250.0):
        assert tile.query_bbox.west >= bbox.west
        assert tile.query_bbox.south >= bbox.south
        assert tile.query_bbox.east <= bbox.east
        assert tile.query_bbox.north <= bbox.north


def test_build_tiles_single_tile_when_area_is_smaller_than_tile_size():
    tiles = build_tiles(BBox.parse("-3.29,51.38,-3.28,51.39"), 5000.0, 100.0)
    assert len(tiles) == 1
    assert tiles[0].tile_id == "r00_c00"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_geo.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.geo'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/geo.py`. This is a move of `parse_bbox`, `validate_bbox`, `bbox_to_dict`, `bbox_to_str`, `lonlat_to_local_meters`, `local_meters_to_lonlat`, `clamp_bbox`, `build_tiles`, `Tile` and `summarise_extent` from `osm_overture_tiles.py`, reshaped around a `BBox` dataclass. The tiling arithmetic is copied unchanged.

```python
"""Bounding boxes, tiling, and the local planar projection.

The projection is an equirectangular approximation referenced to the centre
latitude of the study area. It is accurate at the latitudes and areas this tool
is used for. It degenerates near the poles and does not handle a bbox crossing
the antimeridian. Both are out of scope, see the phase 1 spec.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6378137.0


class BBoxError(ValueError):
    """Raised when a bounding box is malformed or geographically impossible."""


@dataclass(frozen=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    @classmethod
    def parse(cls, value: str) -> "BBox":
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 4:
            raise BBoxError(
                "BBox must contain 4 comma-separated numbers: west,south,east,north"
            )
        try:
            raw_west, raw_south, raw_east, raw_north = [float(part) for part in parts]
        except ValueError as exc:
            raise BBoxError(f"Invalid bbox: {value}") from exc

        west, east = sorted((raw_west, raw_east))
        south, north = sorted((raw_south, raw_north))
        return cls(west=west, south=south, east=east, north=north).validated()

    def validated(self) -> "BBox":
        if not (-180.0 <= self.west <= 180.0 and -180.0 <= self.east <= 180.0):
            raise BBoxError("Longitude values must be between -180 and 180.")
        if not (-90.0 <= self.south <= 90.0 and -90.0 <= self.north <= 90.0):
            raise BBoxError("Latitude values must be between -90 and 90.")
        if self.west == self.east or self.south == self.north:
            raise BBoxError("BBox has zero width or height.")
        return self

    def as_tuple(self) -> tuple[float, float, float, float]:
        return self.west, self.south, self.east, self.north

    def to_dict(self) -> dict[str, float]:
        return {
            "west": round(self.west, 7),
            "south": round(self.south, 7),
            "east": round(self.east, 7),
            "north": round(self.north, 7),
        }

    def to_query_string(self) -> str:
        return ",".join(f"{value:.7f}" for value in self.as_tuple())

    @property
    def centre(self) -> tuple[float, float]:
        return (self.west + self.east) / 2.0, (self.south + self.north) / 2.0


@dataclass(frozen=True)
class Tile:
    tile_id: str
    row: int
    col: int
    core_bbox: BBox
    query_bbox: BBox

    def to_dict(self) -> dict[str, object]:
        return {
            "tile_id": self.tile_id,
            "row": self.row,
            "col": self.col,
            "core_bbox": self.core_bbox.to_dict(),
            "query_bbox": self.query_bbox.to_dict(),
        }


def lonlat_to_local_metres(lon: float, lat: float, ref_lat: float) -> tuple[float, float]:
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def local_metres_to_lonlat(x: float, y: float, ref_lat: float) -> tuple[float, float]:
    lon = math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(ref_lat))))
    lat = math.degrees(y / EARTH_RADIUS_M)
    return lon, lat


def extent_metres(bbox: BBox) -> tuple[float, float]:
    ref_lat = (bbox.south + bbox.north) / 2.0
    x_min, y_min = lonlat_to_local_metres(bbox.west, bbox.south, ref_lat)
    x_max, y_max = lonlat_to_local_metres(bbox.east, bbox.north, ref_lat)
    return x_max - x_min, y_max - y_min


def _clamp(bbox: BBox, bounds: BBox) -> BBox:
    return BBox(
        west=max(bounds.west, bbox.west),
        south=max(bounds.south, bbox.south),
        east=min(bounds.east, bbox.east),
        north=min(bounds.north, bbox.north),
    )


def build_tiles(bbox: BBox, tile_size_m: float, overlap_m: float) -> list[Tile]:
    ref_lat = (bbox.south + bbox.north) / 2.0
    x_min, y_min = lonlat_to_local_metres(bbox.west, bbox.south, ref_lat)
    x_max, y_max = lonlat_to_local_metres(bbox.east, bbox.north, ref_lat)

    cols = math.ceil((x_max - x_min) / tile_size_m)
    rows = math.ceil((y_max - y_min) / tile_size_m)
    tiles: list[Tile] = []

    for row in range(rows):
        core_y_min = y_min + row * tile_size_m
        core_y_max = min(core_y_min + tile_size_m, y_max)

        for col in range(cols):
            core_x_min = x_min + col * tile_size_m
            core_x_max = min(core_x_min + tile_size_m, x_max)

            core_west, core_south = local_metres_to_lonlat(core_x_min, core_y_min, ref_lat)
            core_east, core_north = local_metres_to_lonlat(core_x_max, core_y_max, ref_lat)
            core_bbox = BBox(core_west, core_south, core_east, core_north)

            query_bbox = _clamp(
                BBox(
                    *local_metres_to_lonlat(
                        max(x_min, core_x_min - overlap_m),
                        max(y_min, core_y_min - overlap_m),
                        ref_lat,
                    ),
                    *local_metres_to_lonlat(
                        min(x_max, core_x_max + overlap_m),
                        min(y_max, core_y_max + overlap_m),
                        ref_lat,
                    ),
                ),
                bbox,
            )

            tiles.append(
                Tile(
                    tile_id=f"r{row:02d}_c{col:02d}",
                    row=row,
                    col=col,
                    core_bbox=core_bbox,
                    query_bbox=query_bbox,
                )
            )

    return tiles
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_geo.py -v`
Expected: 13 passed.

If `test_build_tiles_covers_the_area_in_a_grid` reports a count other than 32, do not change the assertion to match. Compare against the existing `south-wales-package-2/_tilework/manifest.json`, which was generated by the original code at 5000 m and records the authoritative tile count. The migration must reproduce it.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/geo.py tests/test_geo.py
git commit -m "feat(geo): move bbox and tiling into mapgen.geo with tests"
```

---

### Task 4: naming.py, slugs, package paths, and the path length guard

**Files:**
- Create: `src/mapgen/naming.py`
- Test: `tests/test_naming.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `slugify(value: str, field: str, max_length: int = 40) -> str`, raising `NamingError` when the result is empty.
  - `PackagePaths` frozen dataclass with fields `root: Path`, `stem: str`, `layers_dir: Path`, `work_dir: Path`, `survey_json: Path`, `project_setting: Path`.
  - `build_package_paths(output_root: Path, region: str, site: str, survey_date: date, stem_override: str | None = None) -> PackagePaths`. The parameter is `survey_date`, not `date`, so it does not shadow the `date` type imported into the same module. Callers passing it by keyword must use `survey_date=`.
  - `check_path_length(paths: PackagePaths, overture_types: Sequence[str], limit: int = 240) -> None`, raising `PathTooLongError`. It measures the longest path the job will actually produce, which means both the nested `_work/raw/overture/<type>/rNN_cNN.geojson` tile path and `paths.project_setting`. The project setting file embeds the full stem and can be the longer of the two, so checking only the tile path lets real overruns through.
  - `NamingError(ValueError)` and `PathTooLongError(NamingError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_naming.py`:

```python
from datetime import date
from pathlib import Path

import pytest

from mapgen.naming import (
    NamingError,
    PathTooLongError,
    build_package_paths,
    check_path_length,
    slugify,
)


def test_slugify_hyphenates_spaces():
    assert slugify("Barry Waterfront", "site") == "Barry-Waterfront"


def test_slugify_preserves_case():
    assert slugify("South Wales", "region") == "South-Wales"


def test_slugify_drops_punctuation():
    assert slugify("St. Mary's Quay!", "site") == "St-Marys-Quay"


def test_slugify_transliterates_non_ascii():
    assert slugify("Dŵr Cymru", "region") == "Dwr-Cymru"


def test_slugify_collapses_repeated_hyphens():
    assert slugify("Barry  --  Waterfront", "site") == "Barry-Waterfront"


def test_slugify_trims_leading_and_trailing_hyphens():
    assert slugify("  -Barry-  ", "site") == "Barry"


def test_slugify_caps_length_at_forty():
    result = slugify("A" * 60, "site")
    assert len(result) == 40


def test_slugify_does_not_end_on_a_hyphen_after_capping():
    result = slugify("A" * 39 + " Waterfront", "site")
    assert not result.endswith("-")


def test_slugify_rejects_empty_result_naming_the_field():
    with pytest.raises(NamingError, match="site"):
        slugify("!!!", "site")


def test_slugify_rejects_blank_input_naming_the_field():
    with pytest.raises(NamingError, match="region"):
        slugify("   ", "region")


def test_build_package_paths_uses_region_date_site_layout(tmp_path):
    paths = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    assert paths.root == tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    assert paths.stem == "Barry-Waterfront_2026-08-01"
    assert paths.layers_dir == paths.root / "layers"
    assert paths.work_dir == paths.root / "_work"
    assert paths.survey_json == paths.root / "survey.json"
    assert paths.project_setting == paths.root / "Barry-Waterfront_2026-08-01_project_setting.json"


def test_build_package_paths_appends_02_on_collision(tmp_path):
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    first.root.mkdir(parents=True)
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    assert second.root.name == "2026-08-01_Barry-Waterfront_02"
    assert second.stem == "Barry-Waterfront_2026-08-01_02"


def test_build_package_paths_appends_03_on_second_collision(tmp_path):
    for _ in range(2):
        build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1)).root.mkdir(
            parents=True
        )
    third = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1))
    assert third.root.name == "2026-08-01_Barry_03"


def test_build_package_paths_honours_a_stem_override(tmp_path):
    paths = build_package_paths(
        tmp_path, "South Wales", "Barry", date(2026, 8, 1), stem_override="51.39_51.38_-3.28_-3.29"
    )
    assert paths.stem == "51.39_51.38_-3.28_-3.29"
    assert paths.root.name == "2026-08-01_Barry"
    assert paths.project_setting.name == "51.39_51.38_-3.28_-3.29_project_setting.json"


def test_check_path_length_passes_for_a_short_root(tmp_path):
    paths = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1))
    check_path_length(paths, ["building", "infrastructure"])


def test_check_path_length_rejects_a_deep_root():
    deep = Path("C:/") / ("x" * 200)
    paths = build_package_paths(deep, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, ["infrastructure"])
    assert "240" in str(excinfo.value)
    assert "shorter output root" in str(excinfo.value)


def test_check_path_length_boundary_is_inclusive(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1))
    probe = len(str(paths.work_dir / "raw" / "overture" / "infrastructure" / "r00_c00.geojson"))
    check_path_length(paths, ["infrastructure"], limit=probe)
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], limit=probe - 1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_naming.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.naming'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/naming.py`:

```python
"""Slugs, package folder layout, and the Windows path length guard."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

MAX_COMPONENT_LENGTH = 40
DEFAULT_PATH_LIMIT = 240

_DISALLOWED = re.compile(r"[^A-Za-z0-9-]")
_REPEATED_HYPHEN = re.compile(r"-{2,}")

# Characters unicodedata does not decompose into ASCII on its own.
_TRANSLITERATIONS = str.maketrans(
    {
        "ŵ": "w", "Ŵ": "W", "ŷ": "y", "Ŷ": "Y",
        "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
        "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    }
)


class NamingError(ValueError):
    """Raised when a name cannot be turned into a usable path component."""


class PathTooLongError(NamingError):
    """Raised when a job would produce paths beyond the Windows limit."""


def slugify(value: str, field: str, max_length: int = MAX_COMPONENT_LENGTH) -> str:
    decomposed = unicodedata.normalize("NFKD", value.translate(_TRANSLITERATIONS))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    hyphenated = re.sub(r"\s+", "-", ascii_only.strip())
    cleaned = _DISALLOWED.sub("", hyphenated)
    collapsed = _REPEATED_HYPHEN.sub("-", cleaned).strip("-")
    capped = collapsed[:max_length].strip("-")
    if not capped:
        raise NamingError(
            f"The {field} name {value!r} contains no usable characters. "
            f"Use letters or numbers."
        )
    return capped


@dataclass(frozen=True)
class PackagePaths:
    root: Path
    stem: str
    layers_dir: Path
    work_dir: Path
    survey_json: Path
    project_setting: Path


def _compose(root: Path, stem: str) -> PackagePaths:
    return PackagePaths(
        root=root,
        stem=stem,
        layers_dir=root / "layers",
        work_dir=root / "_work",
        survey_json=root / "survey.json",
        project_setting=root / f"{stem}_project_setting.json",
    )


def build_package_paths(
    output_root: Path,
    region: str,
    site: str,
    survey_date: date,
    stem_override: str | None = None,
) -> PackagePaths:
    region_slug = slugify(region, "region")
    site_slug = slugify(site, "site")
    date_str = survey_date.isoformat()

    region_dir = Path(output_root) / region_slug
    base_name = f"{date_str}_{site_slug}"

    root = region_dir / base_name
    suffix = ""
    counter = 2
    while root.exists():
        suffix = f"_{counter:02d}"
        root = region_dir / f"{base_name}{suffix}"
        counter += 1

    stem = stem_override if stem_override else f"{site_slug}_{date_str}{suffix}"
    return _compose(root, stem)


def check_path_length(
    paths: PackagePaths,
    overture_types: Sequence[str],
    limit: int = DEFAULT_PATH_LIMIT,
) -> None:
    longest_type = max(overture_types, key=len) if overture_types else "overture"
    worst_case = paths.work_dir / "raw" / "overture" / longest_type / "r00_c00.geojson"
    length = len(str(worst_case.resolve() if not worst_case.is_absolute() else worst_case))
    if length > limit:
        raise PathTooLongError(
            f"This job would produce paths of {length} characters, over the "
            f"{limit} character limit. The longest path would be:\n  {worst_case}\n"
            f"Choose a shorter output root, or shorten the region or site name."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_naming.py -v`
Expected: 17 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/naming.py tests/test_naming.py
git commit -m "feat(naming): slugs, package paths, collision suffixes, path length guard"
```

---

### Task 5: fsutil.py, atomic writes

**Files:**
- Create: `src/mapgen/fsutil.py`
- Test: `tests/test_fsutil.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ensure_dir(path: Path) -> None`
  - `atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None`
  - `atomic_write_bytes(path: Path, data: bytes) -> None`
  - `atomic_writer(path: Path, encoding: str = "utf-8")` context manager yielding a text file handle, renaming on clean exit and discarding the temp file on exception.
  - `best_effort_rmtree(path: Path) -> None`
  - `work_dir_scope(work_dir: Path, keep: bool)` context manager: creates the directory, removes it on clean exit unless `keep` is true, always retains it on exception.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fsutil.py`:

```python
import pytest

from mapgen.fsutil import (
    atomic_write_bytes,
    atomic_write_text,
    atomic_writer,
    best_effort_rmtree,
    ensure_dir,
    work_dir_scope,
)


def test_atomic_write_text_creates_the_file(tmp_path):
    target = tmp_path / "nested" / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_text_leaves_no_temp_files(tmp_path):
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_atomic_write_text_replaces_existing_content(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_bytes_round_trips(tmp_path):
    target = tmp_path / "out.bin"
    atomic_write_bytes(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"


def test_atomic_writer_writes_on_clean_exit(tmp_path):
    target = tmp_path / "out.txt"
    with atomic_writer(target) as handle:
        handle.write("streamed")
    assert target.read_text(encoding="utf-8") == "streamed"


def test_atomic_writer_leaves_no_file_on_exception(tmp_path):
    target = tmp_path / "out.txt"
    with pytest.raises(RuntimeError):
        with atomic_writer(target) as handle:
            handle.write("partial")
            raise RuntimeError("boom")
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_writer_preserves_existing_file_on_exception(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(RuntimeError):
        with atomic_writer(target) as handle:
            handle.write("partial")
            raise RuntimeError("boom")
    assert target.read_text(encoding="utf-8") == "original"


def test_ensure_dir_is_idempotent(tmp_path):
    target = tmp_path / "a" / "b"
    ensure_dir(target)
    ensure_dir(target)
    assert target.is_dir()


def test_best_effort_rmtree_removes_a_tree(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "b" / "f.txt").write_text("x", encoding="utf-8")
    best_effort_rmtree(tmp_path / "a")
    assert not (tmp_path / "a").exists()


def test_best_effort_rmtree_ignores_a_missing_path(tmp_path):
    best_effort_rmtree(tmp_path / "absent")


def test_work_dir_scope_removes_on_success(tmp_path):
    work = tmp_path / "_work"
    with work_dir_scope(work, keep=False):
        assert work.is_dir()
        (work / "tile.osm").write_text("x", encoding="utf-8")
    assert not work.exists()


def test_work_dir_scope_keeps_when_asked(tmp_path):
    work = tmp_path / "_work"
    with work_dir_scope(work, keep=True):
        (work / "tile.osm").write_text("x", encoding="utf-8")
    assert (work / "tile.osm").exists()


def test_work_dir_scope_always_keeps_on_failure(tmp_path):
    work = tmp_path / "_work"
    with pytest.raises(RuntimeError):
        with work_dir_scope(work, keep=False):
            (work / "tile.osm").write_text("x", encoding="utf-8")
            raise RuntimeError("download failed")
    assert (work / "tile.osm").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fsutil.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.fsutil'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/fsutil.py`:

```python
"""Filesystem primitives. Every write in mapgen goes through here.

Partial files are worse than absent ones, so writes land on a temporary path in
the destination directory and are renamed into place only once complete.
"""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _temp_path(path: Path) -> Path:
    # Unique per call, not per process. Threads share a pid, and this tool runs
    # a job worker alongside a threading HTTP server, so a pid-only temp name
    # lets two threads writing the same target interleave into one file and
    # report success to both callers.
    unique = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    return path.with_name(f"{path.name}.{unique}.part")


@contextmanager
def atomic_writer(path: Path, encoding: str = "utf-8") -> Iterator[IO[str]]:
    ensure_dir(path.parent)
    temp = _temp_path(path)
    try:
        with temp.open("w", encoding=encoding, newline="\n") as handle:
            yield handle
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    with atomic_writer(path, encoding=encoding) as handle:
        handle.write(text)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    ensure_dir(path.parent)
    temp = _temp_path(path)
    try:
        temp.write_bytes(data)
        temp.replace(path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def best_effort_rmtree(path: Path) -> None:
    if not path.exists():
        return
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def work_dir_scope(work_dir: Path, keep: bool) -> Iterator[Path]:
    """Scratch directory for a job.

    Removed on success unless keep is set. Always retained on failure, because
    it is what makes resume possible.
    """
    ensure_dir(work_dir)
    yield work_dir
    if not keep:
        best_effort_rmtree(work_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_fsutil.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/fsutil.py tests/test_fsutil.py
git commit -m "feat(fsutil): atomic writes and the job scratch directory scope"
```

---

### Task 6: merge.py, deduplicating tile seams

**Files:**
- Create: `src/mapgen/merge.py`
- Test: `tests/test_merge.py`

**Interfaces:**
- Consumes: `mapgen.fsutil.atomic_writer` from Task 5.
- Produces:
  - `merge_osm_xml(input_paths: Iterable[Path], output_path: Path) -> int` returning the deduplicated element count.
  - `merge_geojson(input_paths: Iterable[Path], output_path: Path) -> int` returning the deduplicated feature count.
  - `MergeError(RuntimeError)`.
  - `assert_inputs_present(expected: Sequence[Path], force: bool = False) -> list[Path]`, raising `MergeError` when any expected file is missing or zero length unless `force` is set, and returning the files that do exist.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_merge.py`:

```python
import json

import pytest

from mapgen.merge import MergeError, assert_inputs_present, merge_geojson, merge_osm_xml

TILE_A = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="51.38" lon="-3.29"/>
  <node id="2" version="1" lat="51.39" lon="-3.28"/>
  <way id="10" version="1"><nd ref="1"/><nd ref="2"/></way>
</osm>
"""

TILE_B = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="2" version="1" lat="51.39" lon="-3.28"/>
  <node id="3" version="1" lat="51.40" lon="-3.27"/>
  <way id="10" version="1"><nd ref="1"/><nd ref="2"/></way>
</osm>
"""

TILE_C_NEWER = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="7" lat="51.3801" lon="-3.2901"/>
</osm>
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_merge_osm_xml_deduplicates_across_tile_seams(tmp_path):
    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "b.osm", TILE_B)]
    count = merge_osm_xml(inputs, tmp_path / "all.osm")
    assert count == 4  # nodes 1, 2, 3 and way 10


def test_merge_osm_xml_keeps_every_distinct_id(tmp_path):
    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "b.osm", TILE_B)]
    out = tmp_path / "all.osm"
    merge_osm_xml(inputs, out)
    text = out.read_text(encoding="utf-8")
    for node_id in ("1", "2", "3"):
        assert 'id="' + node_id + '"' in text


def test_merge_osm_xml_prefers_the_highest_version(tmp_path):
    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "c.osm", TILE_C_NEWER)]
    out = tmp_path / "all.osm"
    merge_osm_xml(inputs, out)
    text = out.read_text(encoding="utf-8")
    assert 'version="7"' in text
    assert 'lat="51.3801"' in text


def test_merge_osm_xml_output_is_parseable(tmp_path):
    import xml.etree.ElementTree as ET

    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "b.osm", TILE_B)]
    out = tmp_path / "all.osm"
    merge_osm_xml(inputs, out)
    assert ET.parse(out).getroot().tag == "osm"


def test_merge_osm_xml_orders_nodes_before_ways(tmp_path):
    out = tmp_path / "all.osm"
    merge_osm_xml([_write(tmp_path, "a.osm", TILE_A)], out)
    text = out.read_text(encoding="utf-8")
    assert text.index("<node") < text.index("<way")


def _collection(*ids):
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": fid, "properties": {}, "geometry": None}
                for fid in ids
            ],
        }
    )


def test_merge_geojson_deduplicates_by_feature_id(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection("f1", "f2"))
    b = _write(tmp_path, "b.geojson", _collection("f2", "f3"))
    assert merge_geojson([a, b], tmp_path / "merged.geojson") == 3


def test_merge_geojson_writes_a_valid_feature_collection(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection("f1"))
    out = tmp_path / "merged.geojson"
    merge_geojson([a], out)
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["type"] == "FeatureCollection"
    assert len(parsed["features"]) == 1


def test_merge_geojson_handles_an_empty_collection(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection())
    out = tmp_path / "merged.geojson"
    assert merge_geojson([a], out) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["features"] == []


def test_merge_leaves_no_output_when_an_input_is_corrupt(tmp_path):
    good = _write(tmp_path, "a.geojson", _collection())
    bad = _write(tmp_path, "b.geojson", "{not json")
    out = tmp_path / "merged.geojson"
    with pytest.raises(Exception):
        merge_geojson([good, bad], out)
    assert not out.exists()


def test_assert_inputs_present_rejects_a_missing_file(tmp_path):
    present = _write(tmp_path, "a.osm", TILE_A)
    with pytest.raises(MergeError, match="b.osm"):
        assert_inputs_present([present, tmp_path / "b.osm"])


def test_assert_inputs_present_rejects_a_zero_length_file(tmp_path):
    present = _write(tmp_path, "a.osm", TILE_A)
    empty = _write(tmp_path, "b.osm", "")
    with pytest.raises(MergeError, match="b.osm"):
        assert_inputs_present([present, empty])


def test_assert_inputs_present_with_force_returns_only_usable_files(tmp_path):
    present = _write(tmp_path, "a.osm", TILE_A)
    assert assert_inputs_present([present, tmp_path / "b.osm"], force=True) == [present]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_merge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.merge'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/merge.py`. The dedupe strategy is moved from `merge_osm_xml_files` and `merge_overture_geojson_files` in `osm_overture_tiles.py`.

```python
"""Merging overlapping tile downloads into single deduplicated outputs.

Tiles are downloaded with an overlap margin so features near a seam are not
clipped, which means the same feature arrives more than once. OSM elements are
keyed on type and id, keeping the highest version. GeoJSON features are keyed on
feature id, keeping the first seen.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from mapgen.fsutil import atomic_writer

OSM_TYPE_ORDER = ("node", "way", "relation")


class MergeError(RuntimeError):
    """Raised when the inputs to a merge are incomplete or unusable."""


def assert_inputs_present(expected: Sequence[Path], force: bool = False) -> list[Path]:
    usable: list[Path] = []
    problems: list[str] = []
    for path in expected:
        if not path.exists():
            problems.append(f"missing: {path.name}")
        elif path.stat().st_size == 0:
            problems.append(f"empty: {path.name}")
        else:
            usable.append(path)

    if problems and not force:
        joined = "\n  ".join(problems)
        raise MergeError(
            f"Refusing to merge an incomplete tile set:\n  {joined}\n"
            f"Re-run the download to fill the gaps, or pass force to merge anyway "
            f"and record the package as incomplete."
        )
    return usable


def _element_rank(element: ET.Element) -> int:
    try:
        return int(element.get("version", "0"))
    except ValueError:
        return 0


def merge_osm_xml(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, ET.Element] = OrderedDict()

    for input_path in input_paths:
        root = ET.parse(input_path).getroot()
        for element in root:
            if element.tag not in OSM_TYPE_ORDER:
                continue
            element_id = element.get("id")
            if element_id is None:
                continue
            key = f"{element.tag}/{element_id}"
            existing = deduped.get(key)
            if existing is None or _element_rank(element) > _element_rank(existing):
                deduped[key] = element

    ordered = sorted(
        deduped.values(),
        key=lambda el: (OSM_TYPE_ORDER.index(el.tag), int(el.get("id", "0"))),
    )

    with atomic_writer(output_path) as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write('<osm version="0.6" generator="mapgen">\n')
        for element in ordered:
            handle.write("  ")
            handle.write(ET.tostring(element, encoding="unicode"))
            handle.write("\n")
        handle.write("</osm>\n")

    return len(ordered)


def _iter_features(path: Path) -> Iterator[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for index, feature in enumerate(payload.get("features", [])):
        feature_id = feature.get("id") or f"{path.name}:{index}"
        yield str(feature_id), json.dumps(feature, separators=(",", ":"))


def merge_geojson(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, str] = OrderedDict()
    for input_path in input_paths:
        for feature_id, compact in _iter_features(input_path):
            deduped.setdefault(feature_id, compact)

    with atomic_writer(output_path) as handle:
        handle.write('{"type":"FeatureCollection","features":[')
        for index, compact in enumerate(deduped.values()):
            if index:
                handle.write(",")
            handle.write(compact)
        handle.write("]}\n")

    return len(deduped)
```

Note on `test_merge_leaves_no_output_when_an_input_is_corrupt`: `merge_geojson` reads inputs lazily inside the `atomic_writer` block, so the `json.JSONDecodeError` from the corrupt file propagates while the temp file is still open. `atomic_writer` deletes the temp file on exception, which is what makes the assertion hold. Do not restructure this to read all inputs before opening the writer.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_merge.py -v`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/merge.py tests/test_merge.py
git commit -m "feat(merge): deduplicating OSM and GeoJSON merges with completeness checks"
```

---

### Task 7: sources/base.py, the LayerSource protocol and registry

This is the seam phase 2 plugs into. Get the shape right and every future data source is one file.

**Files:**
- Create: `src/mapgen/sources/__init__.py`
- Create: `src/mapgen/sources/base.py`
- Test: `tests/test_sources_base.py`

**Interfaces:**
- Consumes: `mapgen.geo.BBox`, `mapgen.geo.Tile` from Task 3.
- Produces:
  - `Estimate` frozen dataclass: `bytes_estimate: int`, `seconds_estimate: float`.
  - `ProgressSink` protocol with `emit(self, event: str, **fields: object) -> None`.
  - `NullProgress`, a concrete no-op `ProgressSink`.
  - `LayerSource` protocol with attributes `id`, `display_name`, `licence`, `attribution`, `requires_api_key`, and methods `estimate`, `fetch`, `merge`.
  - `register(source) -> None`, `get_source(source_id) -> LayerSource`, `available_sources() -> list[LayerSource]`, `clear_registry() -> None`.
  - `UnknownSourceError(KeyError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources_base.py`:

```python
import pytest

from mapgen.geo import BBox, Tile
from mapgen.sources.base import (
    Estimate,
    NullProgress,
    UnknownSourceError,
    available_sources,
    clear_registry,
    get_source,
    register,
)


class FakeSource:
    id = "fake"
    display_name = "Fake Source"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=1024 * len(tiles), seconds_estimate=2.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source="fake", tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir):
        out = out_dir / "fake.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _tile(tile_id, col=0):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=col, core_bbox=bbox, query_bbox=bbox)


def test_registry_starts_empty():
    assert available_sources() == []


def test_register_then_get_returns_the_same_object():
    source = FakeSource()
    register(source)
    assert get_source("fake") is source


def test_available_sources_lists_registered_sources():
    register(FakeSource())
    assert [s.id for s in available_sources()] == ["fake"]


def test_get_source_raises_a_named_error_for_an_unknown_id():
    with pytest.raises(UnknownSourceError, match="nope"):
        get_source("nope")


def test_unknown_source_error_lists_what_is_available():
    register(FakeSource())
    with pytest.raises(UnknownSourceError, match="fake"):
        get_source("nope")


def test_registering_a_duplicate_id_replaces_the_entry():
    register(FakeSource())
    replacement = FakeSource()
    register(replacement)
    assert get_source("fake") is replacement
    assert len(available_sources()) == 1


def test_null_progress_accepts_any_event_without_error():
    NullProgress().emit("anything", a=1, b="two")


def test_a_conforming_source_round_trips_through_the_protocol(tmp_path):
    source = FakeSource()
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    tiles = [_tile("r00_c00", 0), _tile("r00_c01", 1)]

    assert source.estimate(bbox, tiles).bytes_estimate == 2048

    parts = source.fetch(bbox, tiles, tmp_path / "work", NullProgress())
    assert len(parts) == 2

    outputs = source.merge(parts, tmp_path / "out")
    assert outputs[0].read_text(encoding="utf-8") == "r00_c00\nr00_c01"


def test_progress_sink_receives_emitted_events(tmp_path):
    events = []

    class Recorder:
        def emit(self, event, **fields):
            events.append((event, fields))

    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    FakeSource().fetch(bbox, [_tile("r00_c00")], tmp_path / "work", Recorder())

    assert events == [("tile_done", {"source": "fake", "tile_id": "r00_c00"})]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.sources'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/sources/__init__.py` as an empty file, then create `src/mapgen/sources/base.py`:

```python
"""The LayerSource contract.

Every data source implements this protocol, and package.py orchestrates without
knowing about any of them specifically. Adding a source is one new module plus
one register call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from mapgen.geo import BBox, Tile


@dataclass(frozen=True)
class Estimate:
    bytes_estimate: int
    seconds_estimate: float


class UnknownSourceError(KeyError):
    """Raised when a source id is not in the registry."""


@runtime_checkable
class ProgressSink(Protocol):
    def emit(self, event: str, **fields: object) -> None: ...


class NullProgress:
    """Discards every event. Used by tests and non-interactive CLI runs."""

    def emit(self, event: str, **fields: object) -> None:
        return None


@runtime_checkable
class LayerSource(Protocol):
    id: str
    display_name: str
    licence: str
    attribution: str
    requires_api_key: bool

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate: ...

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]: ...

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]: ...


_REGISTRY: dict[str, LayerSource] = {}


def register(source: LayerSource) -> None:
    _REGISTRY[source.id] = source


def get_source(source_id: str) -> LayerSource:
    try:
        return _REGISTRY[source_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none registered"
        raise UnknownSourceError(
            f"Unknown source {source_id!r}. Available sources: {known}."
        ) from None


def available_sources() -> list[LayerSource]:
    return list(_REGISTRY.values())


def clear_registry() -> None:
    _REGISTRY.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_base.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/sources/__init__.py src/mapgen/sources/base.py tests/test_sources_base.py
git commit -m "feat(sources): LayerSource protocol and registry, the phase 2 seam"
```

---

### Task 8: sources/osm.py, the OSM source with rate limiting

**Files:**
- Create: `src/mapgen/sources/osm.py`
- Test: `tests/test_sources_osm.py`

**Interfaces:**
- Consumes: `mapgen.geo.BBox`/`Tile`, `mapgen.sources.base.{Estimate, LayerSource, ProgressSink}`, `mapgen.merge.merge_osm_xml`, `mapgen.fsutil.atomic_write_text`.
- Produces:
  - `OsmSource` class implementing `LayerSource` with `id = "osm"`. Constructor signature: `OsmSource(session=None, overpass_urls=None, osm_api_url=DEFAULT_OSM_API_URL, max_retries=4, timeout_seconds=180, min_interval_seconds=2.0, sleeper=time.sleep, use_overpass=False)`.
  - `OsmSource.endpoints_used: list[str]`, populated during `fetch`, read by Task 13 when writing `survey.json`.
  - `build_overpass_query(bbox: BBox, timeout_seconds: int) -> str`.
  - `retry_delay_seconds(response_headers: Mapping[str, str] | None, attempt: int) -> float`, honouring `Retry-After` when present and using exponential backoff otherwise.
  - `RateLimiter` class with `wait()`, spacing calls to at least `min_interval_seconds`.
  - `OsmDownloadError(RuntimeError)`.
  - Module-level `register(OsmSource())` is NOT called here. Registration happens in Task 12 so tests stay isolated.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources_osm.py`:

```python
import pytest

from mapgen.geo import BBox, Tile
from mapgen.sources.base import NullProgress
from mapgen.sources.osm import (
    OsmDownloadError,
    OsmSource,
    RateLimiter,
    build_overpass_query,
    retry_delay_seconds,
)

OSM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="51.38" lon="-3.29"/>
</osm>
"""


class FakeResponse:
    def __init__(self, status_code=200, text=OSM_XML, headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Replays a queued list of responses and records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._responses.pop(0)


def _tile(tile_id="r00_c00"):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=0, core_bbox=bbox, query_bbox=bbox)


def _source(responses, **kwargs):
    kwargs.setdefault("sleeper", lambda _seconds: None)
    kwargs.setdefault("min_interval_seconds", 0.0)
    return OsmSource(session=FakeSession(responses), **kwargs)


def test_declares_its_identity_and_licence():
    source = OsmSource()
    assert source.id == "osm"
    assert source.display_name
    assert "ODbL" in source.licence
    assert "OpenStreetMap" in source.attribution
    assert source.requires_api_key is False


def test_overpass_query_contains_the_bbox_in_south_west_north_east_order():
    query = build_overpass_query(BBox.parse("-3.29,51.38,-3.28,51.39"), 180)
    assert "51.3800000,-3.2900000,51.3900000,-3.2800000" in query
    assert "[out:xml][timeout:180]" in query


def test_fetch_writes_one_file_per_tile(tmp_path):
    source = _source([FakeResponse()])
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert len(paths) == 1
    assert paths[0].name == "r00_c00.osm"
    assert paths[0].read_text(encoding="utf-8") == OSM_XML


def test_fetch_records_the_endpoint_that_served_each_tile(tmp_path):
    source = _source([FakeResponse()])
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert len(source.endpoints_used) == 1


def test_fetch_emits_progress_per_tile(tmp_path):
    events = []

    class Recorder:
        def emit(self, event, **fields):
            events.append(event)

    source = _source([FakeResponse()])
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, Recorder())
    assert "tile_done" in events


def test_fetch_skips_a_tile_that_is_already_downloaded(tmp_path):
    (tmp_path / "r00_c00.osm").write_text(OSM_XML, encoding="utf-8")
    source = _source([])  # no responses queued: a request would raise IndexError
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0].exists()
    assert source.session.calls == []


def test_fetch_retries_then_succeeds(tmp_path):
    source = _source([FakeResponse(status_code=504, text="gateway"), FakeResponse()])
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0].read_text(encoding="utf-8") == OSM_XML
    assert len(source.session.calls) == 2


def test_fetch_raises_after_exhausting_retries(tmp_path):
    source = _source([FakeResponse(status_code=504, text="gateway")] * 4, max_retries=4)
    with pytest.raises(OsmDownloadError, match="r00_c00"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_node_limit_failure_is_reported_immediately_without_retrying(tmp_path):
    source = _source(
        [FakeResponse(status_code=400, text="You requested too many nodes")], max_retries=4
    )
    with pytest.raises(OsmDownloadError, match="50000"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert len(source.session.calls) == 1


def test_retry_delay_honours_retry_after_in_seconds():
    assert retry_delay_seconds({"Retry-After": "42"}, attempt=1) == pytest.approx(42.0)


def test_retry_delay_ignores_an_unparseable_retry_after():
    assert retry_delay_seconds({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, 1) > 0


def test_retry_delay_backs_off_exponentially_without_a_header():
    first = retry_delay_seconds(None, attempt=1)
    second = retry_delay_seconds(None, attempt=2)
    third = retry_delay_seconds(None, attempt=3)
    assert first < second < third


def test_rate_limiter_waits_between_calls():
    slept = []
    clock = iter([0.0, 0.0, 0.5, 0.5])
    limiter = RateLimiter(
        min_interval_seconds=2.0, sleeper=slept.append, clock=lambda: next(clock)
    )
    limiter.wait()
    limiter.wait()
    assert slept == [pytest.approx(1.5)]


def test_rate_limiter_does_not_wait_when_enough_time_has_passed():
    slept = []
    clock = iter([0.0, 0.0, 10.0, 10.0])
    limiter = RateLimiter(
        min_interval_seconds=2.0, sleeper=slept.append, clock=lambda: next(clock)
    )
    limiter.wait()
    limiter.wait()
    assert slept == []


def test_estimate_scales_with_tile_count():
    source = OsmSource()
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one = source.estimate(bbox, [_tile("r00_c00")])
    two = source.estimate(bbox, [_tile("r00_c00"), _tile("r00_c01")])
    assert two.bytes_estimate > one.bytes_estimate
    assert two.seconds_estimate > one.seconds_estimate


def test_merge_produces_a_single_osm_file(tmp_path):
    part = tmp_path / "r00_c00.osm"
    part.write_text(OSM_XML, encoding="utf-8")
    outputs = OsmSource().merge([part], tmp_path / "out")
    assert len(outputs) == 1
    assert outputs[0].name == "all.osm"
    assert outputs[0].exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_osm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.sources.osm'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/sources/osm.py`. The download loop and Overpass query are moved from `download_osm_tile` and `build_overpass_query` in `osm_overture_tiles.py`, with rate limiting and `Retry-After` handling added.

```python
"""OpenStreetMap as a LayerSource.

Standard .osm XML comes from the OSM map API, which caps a request at 50000
nodes. Overpass is the fallback and the only option for JSON. Both are free
public services, so requests are spaced out and back off on failure.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

import requests

from mapgen.fsutil import atomic_write_text
from mapgen.geo import BBox, Tile
from mapgen.merge import merge_osm_xml
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
DEFAULT_OSM_API_URL = "https://api.openstreetmap.org/api/0.6/map"
USER_AGENT = "mapgen/1.0 (architectural survey tool)"

# Rough bytes per tile at 2000 m, from the South Wales reference package.
BYTES_PER_TILE_ESTIMATE = 3_500_000
SECONDS_PER_TILE_ESTIMATE = 14.0


class OsmDownloadError(RuntimeError):
    """Raised when a tile could not be downloaded."""


def build_overpass_query(bbox: BBox, timeout_seconds: int) -> str:
    south, west = f"{bbox.south:.7f}", f"{bbox.west:.7f}"
    north, east = f"{bbox.north:.7f}", f"{bbox.east:.7f}"
    area = f"{south},{west},{north},{east}"
    return (
        f"[out:xml][timeout:{timeout_seconds}];\n"
        f"(\n"
        f"  node({area});\n"
        f"  way({area});\n"
        f"  relation({area});\n"
        f");\n"
        f"(._;>;);\n"
        f"out meta;"
    )


def retry_delay_seconds(
    response_headers: Mapping[str, str] | None, attempt: int
) -> float:
    if response_headers:
        raw = response_headers.get("Retry-After")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return float(min(60, 2**attempt))


class RateLimiter:
    """Spaces calls so a free public API is not hammered."""

    def __init__(
        self,
        min_interval_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self._sleeper = sleeper
        self._clock = clock
        self._last_call: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval_seconds - elapsed
            if remaining > 0:
                self._sleeper(remaining)
        self._last_call = self._clock()


class OsmSource:
    id = "osm"
    display_name = "OpenStreetMap"
    licence = "Open Database License (ODbL) 1.0"
    attribution = "(c) OpenStreetMap contributors"
    requires_api_key = False

    def __init__(
        self,
        session: object | None = None,
        overpass_urls: Sequence[str] | None = None,
        osm_api_url: str = DEFAULT_OSM_API_URL,
        max_retries: int = 4,
        timeout_seconds: int = 180,
        min_interval_seconds: float = 2.0,
        sleeper: Callable[[float], None] = time.sleep,
        use_overpass: bool = False,
    ) -> None:
        self.session = session if session is not None else requests.Session()
        self.overpass_urls = list(overpass_urls or DEFAULT_OVERPASS_URLS)
        self.osm_api_url = osm_api_url
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.use_overpass = use_overpass
        self._sleeper = sleeper
        self._limiter = RateLimiter(min_interval_seconds, sleeper=sleeper)
        self.endpoints_used: list[str] = []

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        return Estimate(
            bytes_estimate=BYTES_PER_TILE_ESTIMATE * len(tiles),
            seconds_estimate=SECONDS_PER_TILE_ESTIMATE * len(tiles),
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]:
        paths: list[Path] = []
        for tile in tiles:
            output_path = work_dir / f"{tile.tile_id}.osm"
            if output_path.exists() and output_path.stat().st_size > 0:
                progress.emit("tile_skipped", source=self.id, tile_id=tile.tile_id)
                paths.append(output_path)
                continue
            self._download_tile(tile, output_path)
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(output_path)
        return paths

    def _download_tile(self, tile: Tile, output_path: Path) -> None:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            endpoint = self.overpass_urls[(attempt - 1) % len(self.overpass_urls)]
            self._limiter.wait()
            try:
                response = self._request(tile, endpoint)
            except Exception as exc:
                last_error = exc
                self._sleeper(retry_delay_seconds(None, attempt))
                continue

            if response.status_code == 200:
                atomic_write_text(output_path, response.text)
                self.endpoints_used.append(
                    self.osm_api_url if not self.use_overpass else endpoint
                )
                return

            if response.status_code == 400 and "too many nodes" in response.text.lower():
                raise OsmDownloadError(
                    f"Tile {tile.tile_id} exceeded the OSM API 50000-node limit. "
                    f"Reduce the tile size, for example to 1500 or 2000 metres in "
                    f"dense urban areas."
                )

            last_error = OsmDownloadError(
                f"HTTP {response.status_code} for tile {tile.tile_id} via {endpoint}"
            )
            self._sleeper(retry_delay_seconds(response.headers, attempt))

        raise OsmDownloadError(
            f"Failed to download OSM tile {tile.tile_id} after "
            f"{self.max_retries} attempts."
        ) from last_error

    def _request(self, tile: Tile, endpoint: str):
        headers = {"User-Agent": USER_AGENT}
        if not self.use_overpass:
            return self.session.get(
                self.osm_api_url,
                params={"bbox": tile.query_bbox.to_query_string()},
                headers=headers,
                timeout=(30, self.timeout_seconds + 60),
            )
        query = build_overpass_query(tile.query_bbox, self.timeout_seconds)
        return self.session.post(
            endpoint,
            data=query.encode("utf-8"),
            headers={**headers, "Content-Type": "text/plain; charset=utf-8"},
            timeout=(30, self.timeout_seconds + 60),
        )

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]:
        output = out_dir / "all.osm"
        merge_osm_xml(parts, output)
        return [output]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_osm.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/sources/osm.py tests/test_sources_osm.py
git commit -m "feat(sources): OSM source with rate limiting, Retry-After, and per-tile resume"
```

---

### Task 9: sources/overture.py, the Overture source

**Files:**
- Create: `src/mapgen/sources/overture.py`
- Test: `tests/test_sources_overture.py`

**Interfaces:**
- Consumes: `mapgen.geo.BBox`/`Tile`, `mapgen.sources.base.{Estimate, ProgressSink}`, `mapgen.merge.merge_geojson`.
- Produces:
  - `OvertureSource(types=DEFAULT_OVERTURE_TYPES, release=None, runner=subprocess.run, executable_finder=shutil.which)` implementing `LayerSource` with `id = "overture"`.
  - `DEFAULT_OVERTURE_TYPES: list[str]`, the eight types the current script uses.
  - `LAYER_FILENAMES: dict[str, str]` mapping Overture type to the phase 1 `layers/` filename: `{"water": "water.geojson", "land_cover": "vegetation.geojson", "land_use": "landuse.geojson"}`.
  - `OvertureError(RuntimeError)`.
  - `OvertureSource.merge` returns one merged GeoJSON per type, named `<type>.geojson`, in `out_dir`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources_overture.py`:

```python
import json

import pytest

from mapgen.geo import BBox, Tile
from mapgen.sources.base import NullProgress
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    OvertureError,
    OvertureSource,
)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records commands and writes a stub GeoJSON at the requested output path."""

    def __init__(self, returncode=0, stderr=""):
        self.commands = []
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self._returncode == 0:
            output = command[command.index("--output") + 1]
            with open(output, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {"type": "Feature", "id": "f1", "properties": {}, "geometry": None}
                        ],
                    },
                    handle,
                )
        return FakeCompleted(self._returncode, stderr=self._stderr)


def _tile(tile_id="r00_c00"):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=0, core_bbox=bbox, query_bbox=bbox)


def _source(runner, types=("water",)):
    return OvertureSource(
        types=list(types), runner=runner, executable_finder=lambda _name: "overturemaps"
    )


def test_declares_its_identity_and_licence():
    source = OvertureSource()
    assert source.id == "overture"
    assert source.licence
    assert source.attribution
    assert source.requires_api_key is False


def test_default_types_match_the_existing_script():
    assert DEFAULT_OVERTURE_TYPES == [
        "building",
        "place",
        "segment",
        "connector",
        "infrastructure",
        "land_use",
        "land_cover",
        "water",
    ]


def test_layer_filenames_map_the_three_phase_one_layers():
    assert LAYER_FILENAMES == {
        "water": "water.geojson",
        "land_cover": "vegetation.geojson",
        "land_use": "landuse.geojson",
    }


def test_fetch_calls_the_cli_once_per_tile_and_type(tmp_path):
    runner = FakeRunner()
    source = _source(runner, types=("water", "building"))
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"),
        [_tile("r00_c00"), _tile("r00_c01")],
        tmp_path,
        NullProgress(),
    )
    assert len(runner.commands) == 4
    assert len(paths) == 4


def test_fetch_writes_into_a_per_type_subfolder(tmp_path):
    source = _source(FakeRunner())
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0] == tmp_path / "water" / "r00_c00.geojson"


def test_fetch_passes_the_query_bbox_to_the_cli(tmp_path):
    runner = FakeRunner()
    _source(runner).fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert "--bbox=-3.2900000,51.3800000,-3.2800000,51.3900000" in runner.commands[0]


def test_fetch_skips_a_type_and_tile_already_downloaded(tmp_path):
    target = tmp_path / "water" / "r00_c00.geojson"
    target.parent.mkdir(parents=True)
    target.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    runner = FakeRunner()
    _source(runner).fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert runner.commands == []


def test_fetch_reports_a_cli_failure_with_its_stderr(tmp_path):
    runner = FakeRunner(returncode=1, stderr="release not found")
    with pytest.raises(OvertureError, match="release not found"):
        _source(runner).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_fetch_points_the_cli_at_a_part_file_not_the_final_path(tmp_path):
    runner = FakeRunner()
    _source(runner).fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    written_to = runner.commands[0][runner.commands[0].index("--output") + 1]
    assert written_to.endswith(".part")


def test_fetch_leaves_no_partial_file_when_the_cli_fails(tmp_path):
    runner = FakeRunner(returncode=1, stderr="boom")
    with pytest.raises(OvertureError):
        _source(runner).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert not (tmp_path / "water" / "r00_c00.geojson").exists()
    assert list((tmp_path / "water").glob("*.part")) == []


def test_fetch_fails_loudly_when_the_cli_exits_cleanly_without_writing(tmp_path):
    class SilentRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            self.commands.append(command)
            return FakeCompleted(0)

    with pytest.raises(OvertureError, match="wrote nothing"):
        _source(SilentRunner()).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_fetch_reports_a_missing_cli_clearly(tmp_path):
    source = OvertureSource(
        types=["water"], runner=FakeRunner(), executable_finder=lambda _name: None
    )
    with pytest.raises(OvertureError, match="overturemaps"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_fetch_includes_the_release_when_configured(tmp_path):
    runner = FakeRunner()
    source = OvertureSource(
        types=["water"],
        release="2026-02-18.0",
        runner=runner,
        executable_finder=lambda _name: "overturemaps",
    )
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert "--release" in runner.commands[0]
    assert "2026-02-18.0" in runner.commands[0]


def test_merge_writes_one_file_per_type(tmp_path):
    work = tmp_path / "work"
    for overture_type in ("water", "building"):
        folder = work / overture_type
        folder.mkdir(parents=True)
        (folder / "r00_c00.geojson").write_text(
            '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
        )

    source = OvertureSource(types=["water", "building"])
    parts = [
        work / "water" / "r00_c00.geojson",
        work / "building" / "r00_c00.geojson",
    ]
    outputs = source.merge(parts, tmp_path / "out")
    assert sorted(p.name for p in outputs) == ["building.geojson", "water.geojson"]


def test_estimate_scales_with_tiles_and_types():
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one_type = OvertureSource(types=["water"]).estimate(bbox, [_tile()])
    two_types = OvertureSource(types=["water", "building"]).estimate(bbox, [_tile()])
    assert two_types.bytes_estimate > one_type.bytes_estimate
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_overture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.sources.overture'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/sources/overture.py`. The CLI invocation is moved from `download_overture_tile` in `osm_overture_tiles.py`.

```python
"""Overture Maps as a LayerSource.

Downloads run through the overturemaps CLI, which handles the cloud-hosted
parquet release. Each type is fetched separately per tile, then merged into one
GeoJSON per type.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Sequence

from mapgen.fsutil import ensure_dir
from mapgen.geo import BBox, Tile
from mapgen.merge import merge_geojson
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OVERTURE_TYPES = [
    "building",
    "place",
    "segment",
    "connector",
    "infrastructure",
    "land_use",
    "land_cover",
    "water",
]

# Overture type to the friendly filename written into the package layers folder.
LAYER_FILENAMES = {
    "water": "water.geojson",
    "land_cover": "vegetation.geojson",
    "land_use": "landuse.geojson",
}

BYTES_PER_TILE_TYPE_ESTIMATE = 900_000
SECONDS_PER_TILE_TYPE_ESTIMATE = 6.0


class OvertureError(RuntimeError):
    """Raised when the overturemaps CLI is missing or fails."""


class OvertureSource:
    id = "overture"
    display_name = "Overture Maps"
    licence = "Overture Maps Foundation, mixed source licences (ODbL and CDLA-Permissive-2.0)"
    attribution = "(c) Overture Maps Foundation"
    requires_api_key = False

    def __init__(
        self,
        types: Sequence[str] | None = None,
        release: str | None = None,
        runner: Callable[..., object] = subprocess.run,
        executable_finder: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.types = list(types or DEFAULT_OVERTURE_TYPES)
        self.release = release
        self._runner = runner
        self._find = executable_finder

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        units = len(tiles) * len(self.types)
        return Estimate(
            bytes_estimate=BYTES_PER_TILE_TYPE_ESTIMATE * units,
            seconds_estimate=SECONDS_PER_TILE_TYPE_ESTIMATE * units,
        )

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]:
        paths: list[Path] = []
        for tile in tiles:
            for overture_type in self.types:
                output_path = work_dir / overture_type / f"{tile.tile_id}.geojson"
                if output_path.exists() and output_path.stat().st_size > 0:
                    progress.emit(
                        "tile_skipped",
                        source=self.id,
                        tile_id=tile.tile_id,
                        overture_type=overture_type,
                    )
                    paths.append(output_path)
                    continue
                self._download(tile, overture_type, output_path)
                progress.emit(
                    "tile_done",
                    source=self.id,
                    tile_id=tile.tile_id,
                    overture_type=overture_type,
                )
                paths.append(output_path)
        return paths

    def _download(self, tile: Tile, overture_type: str, output_path: Path) -> None:
        executable = self._find("overturemaps")
        if not executable:
            raise OvertureError(
                "Could not find the overturemaps CLI on PATH. Run bootstrap.ps1, "
                "or install it with: pip install overturemaps"
            )

        ensure_dir(output_path.parent)
        # The CLI writes wherever we point it, and a killed process leaves a
        # truncated file that resume would later mistake for a finished tile.
        # Point it at a .part path and rename only once it exits cleanly.
        temp_path = output_path.with_suffix(".geojson.part")
        temp_path.unlink(missing_ok=True)
        command = [
            executable,
            "download",
            f"--bbox={tile.query_bbox.to_query_string()}",
            "-f",
            "geojson",
            "--type",
            overture_type,
            "--output",
            str(temp_path),
        ]
        if self.release:
            command.extend(["--release", self.release])

        result = self._runner(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            temp_path.unlink(missing_ok=True)
            detail = (result.stderr or result.stdout or "").strip()
            raise OvertureError(
                f"overturemaps failed for tile {tile.tile_id}, type {overture_type}: {detail}"
            )

        if not temp_path.exists():
            raise OvertureError(
                f"overturemaps exited cleanly but wrote nothing for tile "
                f"{tile.tile_id}, type {overture_type}."
            )
        temp_path.replace(output_path)

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]:
        by_type: dict[str, list[Path]] = {}
        for part in parts:
            by_type.setdefault(part.parent.name, []).append(part)

        outputs: list[Path] = []
        for overture_type, type_parts in sorted(by_type.items()):
            output = out_dir / f"{overture_type}.geojson"
            merge_geojson(type_parts, output)
            outputs.append(output)
        return outputs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_overture.py -v`
Expected: 15 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/sources/overture.py tests/test_sources_overture.py
git commit -m "feat(sources): Overture source with per-type merge and resume"
```

---

### Task 10: sources/elevation.py, the OpenTopography DEM

**Files:**
- Create: `src/mapgen/sources/elevation.py`
- Test: `tests/test_sources_elevation.py`

**Interfaces:**
- Consumes: `mapgen.geo.BBox`/`Tile`, `mapgen.sources.base.{Estimate, ProgressSink}`, `mapgen.fsutil.atomic_write_bytes`.
- Produces:
  - `ElevationSource(api_key=None, demtype="COP30", session=None, url=DEFAULT_OPENTOPOGRAPHY_URL, timeout_seconds=600)` implementing `LayerSource` with `id = "elevation"` and `requires_api_key = True`.
  - `is_tiff(header: bytes) -> bool`.
  - `resolve_api_key(explicit: str | None = None, environ: Mapping[str, str] | None = None) -> str`, checking `OPENTOPOGRAPHY_API_KEY` then `OPENTOPO_API_KEY`, raising `MissingApiKeyError`.
  - `ElevationError(RuntimeError)` and `MissingApiKeyError(ElevationError)`.
  - `fetch` writes a single `elevation.tif` into `work_dir`, ignoring `tiles` since the DEM is requested for the whole bbox in one call.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sources_elevation.py`:

```python
import pytest

from mapgen.geo import BBox
from mapgen.sources.base import NullProgress
from mapgen.sources.elevation import (
    ElevationError,
    ElevationSource,
    MissingApiKeyError,
    is_tiff,
    resolve_api_key,
)

TIFF_LITTLE_ENDIAN = b"II*\x00" + b"\x00" * 128
TIFF_BIG_ENDIAN = b"MM\x00*" + b"\x00" * 128
BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")


class FakeStreamResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self._response


def test_declares_that_it_needs_an_api_key():
    source = ElevationSource(api_key="k")
    assert source.id == "elevation"
    assert source.requires_api_key is True
    assert source.licence


def test_is_tiff_accepts_both_byte_orders():
    assert is_tiff(TIFF_LITTLE_ENDIAN)
    assert is_tiff(TIFF_BIG_ENDIAN)


def test_is_tiff_rejects_html():
    assert not is_tiff(b"<html><body>error</body></html>")


def test_resolve_api_key_prefers_the_explicit_value():
    assert resolve_api_key("explicit", {"OPENTOPOGRAPHY_API_KEY": "env"}) == "explicit"


def test_resolve_api_key_reads_the_primary_variable():
    assert resolve_api_key(None, {"OPENTOPOGRAPHY_API_KEY": "env"}) == "env"


def test_resolve_api_key_reads_the_legacy_variable():
    assert resolve_api_key(None, {"OPENTOPO_API_KEY": "legacy"}) == "legacy"


def test_resolve_api_key_names_the_variable_when_absent():
    with pytest.raises(MissingApiKeyError, match="OPENTOPOGRAPHY_API_KEY"):
        resolve_api_key(None, {})


def test_fetch_writes_a_single_tiff(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    source = ElevationSource(api_key="k", session=session)
    paths = source.fetch(BBOX, [], tmp_path, NullProgress())
    assert len(paths) == 1
    assert paths[0].name == "elevation.tif"
    assert paths[0].read_bytes() == TIFF_LITTLE_ENDIAN


def test_fetch_sends_the_bbox_and_key_as_parameters(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    ElevationSource(api_key="secret", session=session).fetch(
        BBOX, [], tmp_path, NullProgress()
    )
    params = session.calls[0][1]["params"]
    assert params["API_Key"] == "secret"
    assert params["demtype"] == "COP30"
    assert params["south"].startswith("51.38")
    assert params["north"].startswith("51.39")


def test_fetch_reuses_an_existing_tiff(tmp_path):
    (tmp_path / "elevation.tif").write_bytes(TIFF_LITTLE_ENDIAN)
    session = FakeSession(FakeStreamResponse([]))
    ElevationSource(api_key="k", session=session).fetch(BBOX, [], tmp_path, NullProgress())
    assert session.calls == []


def test_fetch_rejects_a_non_tiff_response_and_leaves_no_file(tmp_path):
    session = FakeSession(FakeStreamResponse([b"<html>rate limited</html>"]))
    source = ElevationSource(api_key="k", session=session)
    with pytest.raises(ElevationError, match="did not return a TIFF"):
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert not (tmp_path / "elevation.tif").exists()


def test_fetch_without_a_key_fails_before_any_request(tmp_path):
    session = FakeSession(FakeStreamResponse([TIFF_LITTLE_ENDIAN]))
    source = ElevationSource(api_key=None, session=session, environ={})
    with pytest.raises(MissingApiKeyError):
        source.fetch(BBOX, [], tmp_path, NullProgress())
    assert session.calls == []


def test_merge_passes_the_tiff_through(tmp_path):
    part = tmp_path / "elevation.tif"
    part.write_bytes(TIFF_LITTLE_ENDIAN)
    assert ElevationSource(api_key="k").merge([part], tmp_path / "out") == [part]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_elevation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.sources.elevation'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/sources/elevation.py`. Moved from `download_opentopography_dem_tiff`, `resolve_opentopography_api_key` and `is_tiff_header` in `osm_overture_tiles.py`.

```python
"""Elevation as a LayerSource, via the OpenTopography global DEM API.

Requested for the whole study bbox in one call rather than per tile, because the
API already handles arbitrary extents and stitching tiled DEMs is needless work.

Phase 2 note: NRW LiDAR at 1 m will be a sibling module here, and is a far better
source than COP30 for anywhere in Wales.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Sequence

import requests

from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox, Tile
from mapgen.sources.base import Estimate, ProgressSink

DEFAULT_OPENTOPOGRAPHY_URL = "https://portal.opentopography.org/API/globaldem"
USER_AGENT = "mapgen/1.0 (architectural survey tool)"
OUTPUT_NAME = "elevation.tif"


class ElevationError(RuntimeError):
    """Raised when the DEM could not be downloaded or was not a TIFF."""


class MissingApiKeyError(ElevationError):
    """Raised when no OpenTopography API key is configured."""


def is_tiff(header: bytes) -> bool:
    return header.startswith(b"II*\x00") or header.startswith(b"MM\x00*")


def resolve_api_key(
    explicit: str | None = None, environ: Mapping[str, str] | None = None
) -> str:
    env = os.environ if environ is None else environ
    key = explicit or env.get("OPENTOPOGRAPHY_API_KEY") or env.get("OPENTOPO_API_KEY")
    if not key:
        raise MissingApiKeyError(
            "Elevation download needs an OpenTopography API key. Set the "
            "OPENTOPOGRAPHY_API_KEY environment variable, or clear the elevation "
            "layer to skip it. Keys are free from portal.opentopography.org."
        )
    return key


class ElevationSource:
    id = "elevation"
    display_name = "Elevation (OpenTopography COP30)"
    licence = "Copernicus DEM, free for any use with attribution"
    attribution = "(c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH"
    requires_api_key = True

    def __init__(
        self,
        api_key: str | None = None,
        demtype: str = "COP30",
        session: object | None = None,
        url: str = DEFAULT_OPENTOPOGRAPHY_URL,
        timeout_seconds: int = 600,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._api_key = api_key
        self.demtype = demtype
        self.session = session if session is not None else requests.Session()
        self.url = url
        self.timeout_seconds = timeout_seconds
        self._environ = environ

    def estimate(self, bbox: BBox, tiles: Sequence[Tile]) -> Estimate:
        return Estimate(bytes_estimate=12_000_000, seconds_estimate=25.0)

    def fetch(
        self,
        bbox: BBox,
        tiles: Sequence[Tile],
        work_dir: Path,
        progress: ProgressSink,
    ) -> list[Path]:
        output_path = work_dir / OUTPUT_NAME
        if output_path.exists() and output_path.stat().st_size > 0:
            progress.emit("tile_skipped", source=self.id, tile_id="whole-area")
            return [output_path]

        api_key = resolve_api_key(self._api_key, self._environ)
        params = {
            "demtype": self.demtype,
            "south": f"{bbox.south:.7f}",
            "north": f"{bbox.north:.7f}",
            "west": f"{bbox.west:.7f}",
            "east": f"{bbox.east:.7f}",
            "outputFormat": "GTiff",
            "API_Key": api_key,
        }

        with self.session.get(
            self.url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            stream=True,
            timeout=self.timeout_seconds,
        ) as response:
            response.raise_for_status()
            payload = b"".join(chunk for chunk in response.iter_content(1024 * 1024) if chunk)

        if not is_tiff(payload[:16]):
            preview = payload[:300].decode("utf-8", errors="replace")
            raise ElevationError(
                f"OpenTopography did not return a TIFF. Response began: {preview}"
            )

        atomic_write_bytes(output_path, payload)
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [output_path]

    def merge(self, parts: Sequence[Path], out_dir: Path) -> list[Path]:
        return list(parts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_sources_elevation.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/sources/elevation.py tests/test_sources_elevation.py
git commit -m "feat(sources): elevation source with API key resolution and TIFF validation"
```

---

### Task 11: jobs.py, resumable state, progress events, cancellation

**Files:**
- Create: `src/mapgen/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `mapgen.fsutil.atomic_write_text`.
- Produces:
  - `JobState` class. Constructor `JobState(state_path: Path, tiles: Sequence[str], source_ids: Sequence[str])`. Classmethod `load_or_create(work_dir, tiles, source_ids) -> JobState`. Methods `mark(tile_id, source_id, status)`, `status(tile_id, source_id) -> str`, `is_done(tile_id, source_id) -> bool`, `save()`, `as_tile_records() -> list[dict[str, object]]`. Property `complete -> bool`.
  - Statuses are the literal strings `"pending"`, `"ok"`, `"failed"`.
  - `Cancelled(Exception)`.
  - `CancelToken` class with `cancel()`, `is_cancelled() -> bool`, `raise_if_cancelled()`.
  - `EventLog` class implementing `ProgressSink`: `emit(event, **fields)` appends to `events: list[dict]` and calls an optional `listener` callback. Used by the web server to stream Server-Sent Events.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_jobs.py`:

```python
import json

import pytest

from mapgen.jobs import CancelToken, Cancelled, EventLog, JobState


def _state(tmp_path, tiles=("r00_c00", "r00_c01"), sources=("osm",)):
    return JobState.load_or_create(tmp_path, list(tiles), list(sources))


def test_new_state_starts_every_tile_pending(tmp_path):
    state = _state(tmp_path)
    assert state.status("r00_c00", "osm") == "pending"
    assert state.is_done("r00_c00", "osm") is False


def test_marking_ok_makes_a_tile_done(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    assert state.is_done("r00_c00", "osm") is True


def test_marking_failed_does_not_make_a_tile_done(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "failed")
    assert state.is_done("r00_c00", "osm") is False
    assert state.status("r00_c00", "osm") == "failed"


def test_state_is_written_to_disk_on_mark(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["tiles"]["r00_c00"]["osm"] == "ok"


def test_state_is_reloaded_from_disk(tmp_path):
    _state(tmp_path).mark("r00_c00", "osm", "ok")
    reloaded = _state(tmp_path)
    assert reloaded.is_done("r00_c00", "osm") is True
    assert reloaded.is_done("r00_c01", "osm") is False


def test_reload_adds_tiles_that_were_not_in_the_saved_state(tmp_path):
    _state(tmp_path, tiles=("r00_c00",)).mark("r00_c00", "osm", "ok")
    widened = _state(tmp_path, tiles=("r00_c00", "r00_c01"))
    assert widened.is_done("r00_c00", "osm") is True
    assert widened.status("r00_c01", "osm") == "pending"


def test_reload_adds_sources_that_were_not_in_the_saved_state(tmp_path):
    _state(tmp_path, sources=("osm",)).mark("r00_c00", "osm", "ok")
    widened = _state(tmp_path, sources=("osm", "overture"))
    assert widened.status("r00_c00", "overture") == "pending"


def test_complete_is_false_while_anything_is_pending(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    assert state.complete is False


def test_complete_is_false_when_anything_failed(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    state.mark("r00_c01", "osm", "failed")
    assert state.complete is False


def test_complete_is_true_when_everything_is_ok(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    state.mark("r00_c01", "osm", "ok")
    assert state.complete is True


def test_as_tile_records_matches_the_survey_json_shape(tmp_path):
    state = _state(tmp_path, tiles=("r00_c00",), sources=("osm", "overture"))
    state.mark("r00_c00", "osm", "ok")
    state.mark("r00_c00", "overture", "failed")
    assert state.as_tile_records() == [
        {"tile_id": "r00_c00", "osm": "ok", "overture": "failed"}
    ]


def test_a_corrupt_state_file_is_discarded_rather_than_crashing(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    state = _state(tmp_path)
    assert state.status("r00_c00", "osm") == "pending"


def test_cancel_token_starts_uncancelled():
    assert CancelToken().is_cancelled() is False


def test_cancel_token_records_cancellation():
    token = CancelToken()
    token.cancel()
    assert token.is_cancelled() is True


def test_raise_if_cancelled_is_silent_when_not_cancelled():
    CancelToken().raise_if_cancelled()


def test_raise_if_cancelled_raises_once_cancelled():
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()


def test_event_log_records_events():
    log = EventLog()
    log.emit("tile_done", tile_id="r00_c00")
    assert log.events == [{"event": "tile_done", "tile_id": "r00_c00"}]


def test_event_log_forwards_to_a_listener():
    seen = []
    log = EventLog(listener=seen.append)
    log.emit("tile_done", tile_id="r00_c00")
    assert seen == [{"event": "tile_done", "tile_id": "r00_c00"}]


def test_event_log_survives_a_listener_that_raises():
    def bad_listener(_payload):
        raise RuntimeError("browser disconnected")

    log = EventLog(listener=bad_listener)
    log.emit("tile_done", tile_id="r00_c00")
    assert len(log.events) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_jobs.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.jobs'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/jobs.py`:

```python
"""Job state, progress events, and cancellation.

State is written after every tile so a job that dies at hour two resumes rather
than restarts. A listener that raises must never take the job down with it,
because the most likely listener is a browser tab that got closed.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Sequence

from mapgen.fsutil import atomic_write_text

STATE_FILENAME = "state.json"
PENDING = "pending"
OK = "ok"
FAILED = "failed"


class Cancelled(Exception):
    """Raised inside a job when the user has asked it to stop."""


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("Job cancelled at the user's request.")


class EventLog:
    """A ProgressSink that keeps history and optionally forwards live."""

    def __init__(self, listener: Callable[[dict], None] | None = None) -> None:
        self.events: list[dict] = []
        self._listener = listener
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: object) -> None:
        payload = {"event": event, **fields}
        with self._lock:
            self.events.append(payload)
        if self._listener is not None:
            try:
                self._listener(payload)
            except Exception:
                # A dead listener is a closed browser tab, not a job failure.
                pass


class JobState:
    def __init__(
        self, state_path: Path, tiles: Sequence[str], source_ids: Sequence[str]
    ) -> None:
        self.state_path = state_path
        self.source_ids = list(source_ids)
        self.tiles: dict[str, dict[str, str]] = {
            tile_id: {source_id: PENDING for source_id in source_ids}
            for tile_id in tiles
        }

    @classmethod
    def load_or_create(
        cls, work_dir: Path, tiles: Sequence[str], source_ids: Sequence[str]
    ) -> "JobState":
        state = cls(work_dir / STATE_FILENAME, tiles, source_ids)
        state._merge_saved()
        return state

    def _merge_saved(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        saved = payload.get("tiles", {})
        for tile_id, sources in self.tiles.items():
            saved_sources = saved.get(tile_id, {})
            for source_id in sources:
                if saved_sources.get(source_id) in (OK, FAILED):
                    sources[source_id] = saved_sources[source_id]

    def mark(self, tile_id: str, source_id: str, status: str) -> None:
        self.tiles.setdefault(tile_id, {})[source_id] = status
        self.save()

    def status(self, tile_id: str, source_id: str) -> str:
        return self.tiles.get(tile_id, {}).get(source_id, PENDING)

    def is_done(self, tile_id: str, source_id: str) -> bool:
        return self.status(tile_id, source_id) == OK

    @property
    def complete(self) -> bool:
        return all(
            status == OK
            for sources in self.tiles.values()
            for status in sources.values()
        )

    def as_tile_records(self) -> list[dict[str, object]]:
        return [
            {"tile_id": tile_id, **sources}
            for tile_id, sources in sorted(self.tiles.items())
        ]

    def save(self) -> None:
        atomic_write_text(
            self.state_path,
            json.dumps({"tiles": self.tiles}, indent=2),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_jobs.py -v`
Expected: 19 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/jobs.py tests/test_jobs.py
git commit -m "feat(jobs): resumable job state, progress events, cancellation token"
```

---

### Task 12: bridge.py, driving UrbanoBridge

**Files:**
- Create: `src/mapgen/bridge.py`
- Test: `tests/test_bridge.py`

**Interfaces:**
- Consumes: `mapgen.geo.BBox`.
- Produces:
  - `BridgeRequest` frozen dataclass: `bbox: BBox`, `output_dir: Path`, `file_name_stem: str | None`, `granularity: str = "Block"`, `osm_file_path: Path | None = None`, `elevation_tiff_path: Path | None = None`, `skip_blocks: bool = True`, `skip_climate: bool = True`, `skip_elevation: bool = False`, `skip_overture: bool = False`, `package_dir: str | None = None`.
  - `build_command(request: BridgeRequest, project_path: Path) -> list[str]`.
  - `run_bridge(request: BridgeRequest, project_path: Path | None = None, runner=subprocess.run) -> None`, raising `BridgeError` on non-zero exit or missing project.
  - `BridgeError(RuntimeError)`.
  - `DEFAULT_PROJECT_PATH: Path`, resolved relative to the repository root as `tools/UrbanoBridge/UrbanoBridge.csproj`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_bridge.py`:

```python
from pathlib import Path

import pytest

from mapgen.bridge import BridgeError, BridgeRequest, build_command, run_bridge
from mapgen.geo import BBox

BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


class FakeRunner:
    def __init__(self, returncode=0):
        self.commands = []
        self._returncode = returncode

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        return FakeCompleted(self._returncode)


def _request(tmp_path, **overrides):
    defaults = dict(
        bbox=BBOX,
        output_dir=tmp_path / "out",
        file_name_stem="Barry-Waterfront_2026-08-01",
    )
    defaults.update(overrides)
    return BridgeRequest(**defaults)


def _project(tmp_path):
    project = tmp_path / "UrbanoBridge.csproj"
    project.write_text("<Project/>", encoding="utf-8")
    return project


def test_command_invokes_dotnet_run_on_the_project(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    assert command[:3] == ["dotnet", "run", "--project"]
    assert command[3].endswith("UrbanoBridge.csproj")
    assert "--" in command


def test_command_passes_the_bbox_as_west_south_east_north(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    index = command.index("--bbox")
    assert command[index + 1] == "-3.2900000,51.3800000,-3.2800000,51.3900000"


def test_command_passes_the_file_name_stem(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    index = command.index("--file-name-stem")
    assert command[index + 1] == "Barry-Waterfront_2026-08-01"


def test_command_omits_the_stem_when_none_so_the_bridge_uses_coordinates(tmp_path):
    command = build_command(_request(tmp_path, file_name_stem=None), _project(tmp_path))
    assert "--file-name-stem" not in command


def test_command_includes_skip_flags_that_are_set(tmp_path):
    command = build_command(
        _request(tmp_path, skip_blocks=True, skip_climate=True, skip_elevation=True),
        _project(tmp_path),
    )
    assert "--skip-blocks" in command
    assert "--skip-climate" in command
    assert "--skip-elevation" in command


def test_command_omits_skip_flags_that_are_not_set(tmp_path):
    command = build_command(
        _request(tmp_path, skip_blocks=False, skip_climate=False, skip_elevation=False),
        _project(tmp_path),
    )
    assert "--skip-blocks" not in command
    assert "--skip-climate" not in command
    assert "--skip-elevation" not in command


def test_command_passes_optional_file_paths(tmp_path):
    osm = tmp_path / "all.osm"
    dem = tmp_path / "elevation.tif"
    command = build_command(
        _request(tmp_path, osm_file_path=osm, elevation_tiff_path=dem), _project(tmp_path)
    )
    assert command[command.index("--osm-file-path") + 1] == str(osm)
    assert command[command.index("--elevation-tiff-path") + 1] == str(dem)


def test_run_bridge_invokes_the_runner(tmp_path):
    runner = FakeRunner()
    run_bridge(_request(tmp_path), _project(tmp_path), runner=runner)
    assert len(runner.commands) == 1


def test_run_bridge_raises_on_a_non_zero_exit(tmp_path):
    with pytest.raises(BridgeError, match="exit code 1"):
        run_bridge(_request(tmp_path), _project(tmp_path), runner=FakeRunner(returncode=1))


def test_run_bridge_reports_a_missing_project_clearly(tmp_path):
    missing = tmp_path / "absent" / "UrbanoBridge.csproj"
    with pytest.raises(BridgeError, match="UrbanoBridge.csproj"):
        run_bridge(_request(tmp_path), missing, runner=FakeRunner())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_bridge.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.bridge'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/bridge.py`. Moved from `run_urbano_bridge` in `osm_overture_tiles.py`, with the new `--file-name-stem` argument from Task 2.

```python
"""Driving the C# UrbanoBridge.

The bridge loads Urbano's own assemblies to produce the .egrid, the geoparquet
and the project setting JSON that Urbano 2 reads. We shell out to it rather than
reimplementing any of that.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mapgen.geo import BBox

DEFAULT_PROJECT_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "UrbanoBridge" / "UrbanoBridge.csproj"
)


class BridgeError(RuntimeError):
    """Raised when the bridge is missing or exits non-zero."""


@dataclass(frozen=True)
class BridgeRequest:
    bbox: BBox
    output_dir: Path
    file_name_stem: str | None
    granularity: str = "Block"
    osm_file_path: Path | None = None
    elevation_tiff_path: Path | None = None
    skip_blocks: bool = True
    skip_climate: bool = True
    skip_elevation: bool = False
    skip_overture: bool = False
    package_dir: str | None = None


def build_command(request: BridgeRequest, project_path: Path) -> list[str]:
    command = [
        "dotnet",
        "run",
        "--project",
        str(project_path),
        "--",
        "--bbox",
        request.bbox.to_query_string(),
        "--output-folder",
        str(request.output_dir),
        "--granularity",
        request.granularity,
    ]

    if request.file_name_stem:
        command.extend(["--file-name-stem", request.file_name_stem])
    if request.package_dir:
        command.extend(["--package-dir", request.package_dir])
    if request.osm_file_path is not None:
        command.extend(["--osm-file-path", str(request.osm_file_path)])
    if request.elevation_tiff_path is not None:
        command.extend(["--elevation-tiff-path", str(request.elevation_tiff_path)])

    for flag, enabled in (
        ("--skip-climate", request.skip_climate),
        ("--skip-blocks", request.skip_blocks),
        ("--skip-elevation", request.skip_elevation),
        ("--skip-overture", request.skip_overture),
    ):
        if enabled:
            command.append(flag)

    return command


def run_bridge(
    request: BridgeRequest,
    project_path: Path | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    project = project_path if project_path is not None else DEFAULT_PROJECT_PATH
    if not project.exists():
        raise BridgeError(
            f"Urbano bridge project not found at {project}. Build it with:\n"
            f"  dotnet build tools/UrbanoBridge/UrbanoBridge.csproj"
        )

    result = runner(build_command(request, project), check=False)
    if result.returncode != 0:
        raise BridgeError(
            f"The Urbano bridge failed with exit code {result.returncode}. "
            f"See the output above for details."
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_bridge.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/mapgen/bridge.py tests/test_bridge.py
git commit -m "feat(bridge): typed request and command builder for UrbanoBridge"
```

---

### Task 13: package.py, orchestration and survey.json

The piece that ties everything together. It knows about paths, state and the registry, and nothing about any specific data source.

**Files:**
- Create: `src/mapgen/package.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: everything from Tasks 3 to 12.
- Produces:
  - `SurveyRequest` frozen dataclass: `bbox: BBox`, `region: str`, `site: str`, `output_root: Path`, `tile_size_m: float = 2000.0`, `overlap_m: float = 100.0`, `source_ids: Sequence[str] = ("osm", "overture")`, `overture_types: Sequence[str] | None = None`, `keep_work: bool = False`, `coordinate_stem: bool = False`, `force: bool = False`, `survey_date: date | None = None`, `run_bridge_step: bool = True`.
  - `SurveyResult` frozen dataclass: `paths: PackagePaths`, `complete: bool`, `survey: dict[str, object]`.
  - `estimate_survey(request: SurveyRequest) -> dict[str, object]` returning `{"tiles": int, "rows": int, "cols": int, "extent_km": {...}, "bytes_estimate": int, "seconds_estimate": float, "sources": [...]}`. Performs the path length guard and raises before any network call.
  - `run_survey(request, progress=None, cancel=None, bridge_runner=None) -> SurveyResult`.
  - `register_default_sources() -> None`, registering `OsmSource`, `OvertureSource` and `ElevationSource`.
  - `build_survey_json(...) -> dict[str, object]` producing the schema in the spec.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_package.py`:

```python
import json
from datetime import date
from pathlib import Path

import pytest

from mapgen.geo import BBox
from mapgen.jobs import CancelToken, Cancelled, EventLog
from mapgen.naming import PathTooLongError
from mapgen.package import (
    SurveyRequest,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import Estimate, clear_registry, register

BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")


class StubSource:
    """Writes one predictable file per tile and merges them by concatenation."""

    def __init__(self, source_id="stub", fail_on=()):
        self.id = source_id
        self.display_name = f"Stub {source_id}"
        self.licence = "CC0"
        self.attribution = "nobody"
        self.requires_api_key = False
        self._fail_on = set(fail_on)

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            if tile.tile_id in self._fail_on:
                raise RuntimeError(f"stub failure on {tile.tile_id}")
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registry()
    yield
    clear_registry()


def _request(tmp_path, **overrides):
    defaults = dict(
        bbox=BBOX,
        region="South Wales",
        site="Barry Waterfront",
        output_root=tmp_path,
        tile_size_m=600.0,
        overlap_m=50.0,
        source_ids=("stub",),
        survey_date=date(2026, 8, 1),
        run_bridge_step=False,
    )
    defaults.update(overrides)
    return SurveyRequest(**defaults)


def test_estimate_reports_tile_count_and_extent(tmp_path):
    register(StubSource())
    estimate = estimate_survey(_request(tmp_path))
    assert estimate["tiles"] >= 1
    assert estimate["extent_km"]["width"] > 0
    assert estimate["bytes_estimate"] > 0


def test_estimate_lists_each_selected_source(tmp_path):
    register(StubSource())
    estimate = estimate_survey(_request(tmp_path))
    assert [s["id"] for s in estimate["sources"]] == ["stub"]


def test_estimate_rejects_a_path_that_would_be_too_long(tmp_path):
    register(StubSource())
    deep = Path("C:/") / ("x" * 200)
    with pytest.raises(PathTooLongError):
        estimate_survey(_request(tmp_path, output_root=deep))


def test_run_creates_the_dated_region_folder(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    assert result.paths.root == tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    assert result.paths.root.is_dir()


def test_run_writes_survey_json_with_the_expected_shape(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["site"] == "Barry Waterfront"
    assert payload["region"] == "South Wales"
    assert payload["slug"] == {"site": "Barry-Waterfront", "region": "South-Wales"}
    assert payload["date"] == "2026-08-01"
    assert payload["urbano_stem"] == "Barry-Waterfront_2026-08-01"
    assert payload["bbox"]["west"] == pytest.approx(-3.29)
    assert payload["complete"] is True
    assert payload["started_at"].endswith("Z")
    assert payload["finished_at"].endswith("Z")


def test_survey_json_records_licence_and_attribution_per_source(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["sources"][0]["licence"] == "CC0"
    assert payload["sources"][0]["attribution"] == "nobody"


def test_survey_json_records_per_tile_outcomes(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert all(record["stub"] == "ok" for record in payload["tiles"])


def test_work_dir_is_removed_on_success(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    assert not result.paths.work_dir.exists()


def test_work_dir_is_kept_when_requested(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, keep_work=True))
    assert result.paths.work_dir.is_dir()


def test_a_failing_tile_marks_the_package_incomplete(tmp_path):
    register(StubSource(fail_on=("r00_c00",)))
    result = run_survey(_request(tmp_path, force=True))
    assert result.complete is False
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["complete"] is False


def test_work_dir_is_retained_after_a_failure_so_the_job_can_resume(tmp_path):
    register(StubSource(fail_on=("r00_c00",)))
    result = run_survey(_request(tmp_path, force=True))
    assert result.paths.work_dir.is_dir()


def test_a_second_run_of_the_same_site_gets_an_02_suffix(tmp_path):
    register(StubSource())
    first = run_survey(_request(tmp_path))
    second = run_survey(_request(tmp_path))
    assert first.paths.root.name == "2026-08-01_Barry-Waterfront"
    assert second.paths.root.name == "2026-08-01_Barry-Waterfront_02"


def test_progress_events_reach_the_sink(tmp_path):
    register(StubSource())
    log = EventLog()
    run_survey(_request(tmp_path), progress=log)
    assert any(e["event"] == "tile_done" for e in log.events)
    assert any(e["event"] == "job_finished" for e in log.events)


def test_cancellation_stops_the_job(tmp_path):
    register(StubSource())
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        run_survey(_request(tmp_path), cancel=token)


def test_coordinate_stem_option_uses_the_coordinate_form(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, coordinate_stem=True))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["urbano_stem"] == "51.39_51.38_-3.28_-3.29"


def test_register_default_sources_registers_the_three_phase_one_sources():
    register_default_sources()
    from mapgen.sources.base import available_sources

    assert sorted(s.id for s in available_sources()) == ["elevation", "osm", "overture"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_package.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.package'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/package.py`:

```python
"""Orchestration: plan, download, merge, bridge, survey.json.

Knows about paths, job state and the source registry. Knows nothing about any
individual data source, which is what makes phase 2 additive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from mapgen import __version__
from mapgen.bridge import BridgeRequest, run_bridge
from mapgen.fsutil import (
    atomic_write_text,
    best_effort_rmtree,
    ensure_dir,
    work_dir_scope,
)
from mapgen.geo import BBox, build_tiles, extent_metres
from mapgen.jobs import FAILED, OK, CancelToken, JobState
from mapgen.merge import assert_inputs_present
from mapgen.naming import PackagePaths, build_package_paths, check_path_length, slugify
from mapgen.sources.base import NullProgress, ProgressSink, get_source, register
from mapgen.sources.elevation import ElevationSource
from mapgen.sources.osm import OsmSource
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    OvertureSource,
)

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SurveyRequest:
    bbox: BBox
    region: str
    site: str
    output_root: Path
    tile_size_m: float = 2000.0
    overlap_m: float = 100.0
    source_ids: Sequence[str] = ("osm", "overture")
    overture_types: Sequence[str] | None = None
    keep_work: bool = False
    coordinate_stem: bool = False
    force: bool = False
    survey_date: date | None = None
    run_bridge_step: bool = True

    @property
    def effective_date(self) -> date:
        return self.survey_date or date.today()

    @property
    def effective_overture_types(self) -> list[str]:
        return list(self.overture_types or DEFAULT_OVERTURE_TYPES)


@dataclass(frozen=True)
class SurveyResult:
    paths: PackagePaths
    complete: bool
    survey: dict[str, object]


def register_default_sources() -> None:
    register(OsmSource())
    register(OvertureSource())
    register(ElevationSource())


def _coordinate_stem(bbox: BBox) -> str:
    def fmt(value: float) -> str:
        return f"{value:.7f}".rstrip("0").rstrip(".")

    return "_".join((fmt(bbox.north), fmt(bbox.south), fmt(bbox.east), fmt(bbox.west)))


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan(request: SurveyRequest):
    tiles = build_tiles(request.bbox, request.tile_size_m, request.overlap_m)
    stem_override = _coordinate_stem(request.bbox) if request.coordinate_stem else None
    paths = build_package_paths(
        request.output_root,
        request.region,
        request.site,
        request.effective_date,
        stem_override=stem_override,
    )
    check_path_length(paths, request.effective_overture_types)
    return tiles, paths


def estimate_survey(request: SurveyRequest) -> dict[str, object]:
    tiles, _paths = _plan(request)
    width_m, height_m = extent_metres(request.bbox)

    total_bytes = 0
    total_seconds = 0.0
    source_summaries: list[dict[str, object]] = []
    for source_id in request.source_ids:
        source = get_source(source_id)
        estimate = source.estimate(request.bbox, tiles)
        total_bytes += estimate.bytes_estimate
        total_seconds += estimate.seconds_estimate
        source_summaries.append(
            {
                "id": source.id,
                "display_name": source.display_name,
                "licence": source.licence,
                "bytes_estimate": estimate.bytes_estimate,
                "seconds_estimate": estimate.seconds_estimate,
            }
        )

    return {
        "tiles": len(tiles),
        "rows": max((t.row for t in tiles), default=0) + 1,
        "cols": max((t.col for t in tiles), default=0) + 1,
        "extent_km": {"width": width_m / 1000.0, "height": height_m / 1000.0},
        "bytes_estimate": total_bytes,
        "seconds_estimate": total_seconds,
        "sources": source_summaries,
    }


def run_survey(
    request: SurveyRequest,
    progress: ProgressSink | None = None,
    cancel: CancelToken | None = None,
    bridge_runner=None,
) -> SurveyResult:
    sink = progress if progress is not None else NullProgress()
    token = cancel if cancel is not None else CancelToken()

    tiles, paths = _plan(request)
    ensure_dir(paths.root)
    ensure_dir(paths.layers_dir)
    started_at = _now()

    sink.emit("job_started", tiles=len(tiles), root=str(paths.root))

    sources = [get_source(source_id) for source_id in request.source_ids]
    state = JobState.load_or_create(
        paths.work_dir, [t.tile_id for t in tiles], [s.id for s in sources]
    )
    ensure_dir(paths.work_dir)

    outputs_by_source: dict[str, list[Path]] = {}

    # The scope always keeps the directory. Removal is decided after the job,
    # by completeness, so a partial run can always be resumed.
    with work_dir_scope(paths.work_dir, keep=True):
        for source in sources:
            token.raise_if_cancelled()
            source_work = paths.work_dir / "raw" / source.id
            ensure_dir(source_work)
            pending = [t for t in tiles if not state.is_done(t.tile_id, source.id)]
            try:
                parts = source.fetch(request.bbox, pending, source_work, sink)
                for tile in pending:
                    state.mark(tile.tile_id, source.id, OK)
            except Exception as exc:
                for tile in pending:
                    state.mark(tile.tile_id, source.id, FAILED)
                sink.emit("source_failed", source=source.id, error=str(exc))
                if not request.force:
                    raise
                parts = [p for p in sorted(source_work.rglob("*")) if p.is_file()]

            # Refuse to merge a tile set with holes unless the caller forced it.
            parts = assert_inputs_present(parts, force=request.force)
            merged = source.merge(parts, paths.root)
            outputs_by_source[source.id] = merged
            sink.emit("source_done", source=source.id, outputs=[p.name for p in merged])

        _write_layer_files(outputs_by_source, paths)

        if request.run_bridge_step:
            token.raise_if_cancelled()
            sink.emit("bridge_started")
            osm_outputs = outputs_by_source.get("osm", [])
            elevation_outputs = outputs_by_source.get("elevation", [])
            bridge_request = BridgeRequest(
                bbox=request.bbox,
                output_dir=paths.root,
                file_name_stem=None if request.coordinate_stem else paths.stem,
                osm_file_path=osm_outputs[0] if osm_outputs else None,
                elevation_tiff_path=elevation_outputs[0] if elevation_outputs else None,
                skip_elevation=not elevation_outputs,
            )
            if bridge_runner is None:
                run_bridge(bridge_request)
            else:
                run_bridge(bridge_request, runner=bridge_runner)
            sink.emit("bridge_done")

    survey = _build_survey_json(request, paths, tiles, sources, state, started_at)
    atomic_write_text(paths.survey_json, json.dumps(survey, indent=2))
    sink.emit("job_finished", complete=state.complete, root=str(paths.root))

    # Removed only on a clean, complete run. A failed or partial job keeps its
    # tiles, because that is what makes the next run resume rather than restart.
    if state.complete and not request.keep_work:
        best_effort_rmtree(paths.work_dir)

    return SurveyResult(paths=paths, complete=state.complete, survey=survey)


def _write_layer_files(
    outputs_by_source: dict[str, list[Path]], paths: PackagePaths
) -> None:
    """Copy the three phase 1 Overture layers into layers/ under friendly names."""
    for output in outputs_by_source.get("overture", []):
        friendly = LAYER_FILENAMES.get(output.stem)
        if friendly:
            atomic_write_text(
                paths.layers_dir / friendly, output.read_text(encoding="utf-8")
            )


def _build_survey_json(request, paths, tiles, sources, state, started_at) -> dict:
    width_m, height_m = extent_metres(request.bbox)
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "site": request.site,
        "region": request.region,
        "slug": {
            "site": slugify(request.site, "site"),
            "region": slugify(request.region, "region"),
        },
        "date": request.effective_date.isoformat(),
        "urbano_stem": paths.stem,
        "bbox": request.bbox.to_dict(),
        "extent_km": {
            "width": round(width_m / 1000.0, 3),
            "height": round(height_m / 1000.0, 3),
        },
        "tiling": {
            "tile_size_m": request.tile_size_m,
            "overlap_m": request.overlap_m,
            "rows": max((t.row for t in tiles), default=0) + 1,
            "cols": max((t.col for t in tiles), default=0) + 1,
        },
        "sources": [
            {
                "id": source.id,
                "licence": source.licence,
                "attribution": source.attribution,
                "endpoints_used": list(getattr(source, "endpoints_used", [])),
            }
            for source in sources
        ],
        "tiles": state.as_tile_records(),
        "complete": state.complete,
        "started_at": started_at,
        "finished_at": _now(),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_package.py -v`
Expected: 16 passed.

Three of these tests pin the `_work` lifecycle and they interact, so if one fails check all three together. `work_dir_scope` is always called with `keep=True`, so it never deletes anything; the single deletion point is the `best_effort_rmtree` after the block, guarded by `state.complete and not request.keep_work`. That gives: complete run removes it, `--keep-work` keeps it, failed or forced-partial run keeps it so the next run resumes. Do not move the deletion back inside the scope.

- [ ] **Step 5: Run the whole suite to check nothing regressed**

Run: `.\.venv\Scripts\python.exe -m pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/mapgen/package.py tests/test_package.py
git commit -m "feat(package): survey orchestration, layers folder, survey.json audit trail"
```

---

### Task 14: cli.py, the command line

**Files:**
- Create: `src/mapgen/cli.py`
- Create: `src/mapgen/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `mapgen.package`, `mapgen.geo.BBox`, `mapgen.config`.
- Produces:
  - `main(argv: Sequence[str] | None = None) -> int`, the console script entry point.
  - `build_parser() -> argparse.ArgumentParser` with subcommands `survey`, `estimate`, `ui`, plus the legacy `plan`, `download`, `merge` and `urbano-package` which forward to the same machinery.
  - `python -m mapgen` works through `__main__.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cli.py`:

```python
import json

import pytest

from mapgen.cli import build_parser, main
from mapgen.sources.base import Estimate, clear_registry, register


class StubSource:
    id = "stub"
    display_name = "Stub"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir):
        out = out_dir / "stub.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registry()
    register(StubSource())
    yield
    clear_registry()


def test_parser_exposes_every_expected_subcommand():
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert set(actions[0].choices) >= {
        "survey",
        "estimate",
        "ui",
        "plan",
        "download",
        "merge",
        "urbano-package",
    }


def test_survey_requires_region_and_site():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["survey", "--bbox=-3.29,51.38,-3.28,51.39"])


def test_estimate_prints_tile_count(tmp_path, capsys):
    exit_code = main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
        ]
    )
    assert exit_code == 0
    assert "Tiles:" in capsys.readouterr().out


def test_estimate_json_output_is_parseable(tmp_path, capsys):
    main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["tiles"] >= 1


def test_survey_creates_the_package_folder(tmp_path):
    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry Waterfront",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
            "--skip-bridge",
            "--date",
            "2026-08-01",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront" / "survey.json").exists()


def test_survey_reports_a_bad_bbox_without_a_traceback(tmp_path, capsys):
    exit_code = main(
        [
            "survey",
            "--bbox=nonsense",
            "--region=R",
            "--site=S",
            "--output-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 2
    assert "bbox" in capsys.readouterr().err.lower()


def test_survey_reports_a_path_that_is_too_long_without_a_traceback(capsys):
    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry Waterfront",
            "--output-root",
            "C:/" + "x" * 200,
            "--source",
            "stub",
        ]
    )
    assert exit_code == 1
    assert "240" in capsys.readouterr().err


def test_coordinate_stem_flag_is_accepted():
    parser = build_parser()
    args = parser.parse_args(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=R",
            "--site=S",
            "--coordinate-stem",
        ]
    )
    assert args.coordinate_stem is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.cli'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/cli.py`:

```python
"""Command line entry point.

The survey subcommand is the one that matters. plan, download, merge and
urbano-package are retained so existing muscle memory and any scripts keep
working, and they route through the same orchestration.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from mapgen import __version__
from mapgen.geo import BBox, BBoxError
from mapgen.naming import NamingError
from mapgen.package import (
    SurveyRequest,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import UnknownSourceError, available_sources


class ConsoleProgress:
    def emit(self, event: str, **fields: object) -> None:
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"[{event}] {detail}".rstrip(), flush=True)


def _parse_bbox(value: str) -> BBox:
    try:
        return BBox.parse(value)
    except BBoxError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _add_survey_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bbox", type=_parse_bbox, required=True,
                        help="west,south,east,north. Reversed pairs are normalised.")
    parser.add_argument("--region", required=True, help="Region folder, for example South Wales.")
    parser.add_argument("--site", required=True, help="Site name, for example Barry Waterfront.")
    parser.add_argument("--output-root", type=Path, default=None,
                        help="Where survey folders are created. Defaults to the saved config.")
    parser.add_argument("--tile-size-m", type=float, default=2000.0)
    parser.add_argument("--overlap-m", type=float, default=100.0)
    parser.add_argument("--source", action="append", dest="sources",
                        help="Repeatable. Defaults to osm and overture.")
    parser.add_argument("--overture-type", action="append", dest="overture_types")
    parser.add_argument("--date", type=_parse_date, default=None,
                        help="Survey date, ISO format. Defaults to today.")
    parser.add_argument("--keep-work", action="store_true",
                        help="Keep the _work tile folder after a successful run.")
    parser.add_argument("--coordinate-stem", action="store_true",
                        help="Use the legacy coordinate file stem instead of the readable one.")
    parser.add_argument("--force", action="store_true",
                        help="Continue past failed tiles and mark the package incomplete.")
    parser.add_argument("--skip-bridge", action="store_true",
                        help="Skip the Urbano bridge step.")


def _request_from_args(args: argparse.Namespace) -> SurveyRequest:
    from mapgen.config import load_config

    output_root = args.output_root or load_config().output_root
    return SurveyRequest(
        bbox=args.bbox,
        region=args.region,
        site=args.site,
        output_root=Path(output_root),
        tile_size_m=args.tile_size_m,
        overlap_m=args.overlap_m,
        source_ids=tuple(args.sources or ("osm", "overture")),
        overture_types=tuple(args.overture_types) if args.overture_types else None,
        keep_work=args.keep_work,
        coordinate_stem=args.coordinate_stem,
        force=args.force,
        survey_date=args.date,
        run_bridge_step=not args.skip_bridge,
    )


def command_estimate(args: argparse.Namespace) -> int:
    estimate = estimate_survey(_request_from_args(args))
    if args.json:
        print(json.dumps(estimate, indent=2))
        return 0
    extent = estimate["extent_km"]
    print(f"Extent: {extent['width']:.2f} km x {extent['height']:.2f} km")
    print(f"Tiles: {estimate['tiles']} ({estimate['rows']} rows x {estimate['cols']} cols)")
    print(f"Estimated download: {estimate['bytes_estimate'] / 1e6:.0f} MB")
    print(f"Estimated duration: {estimate['seconds_estimate'] / 60:.0f} min")
    for source in estimate["sources"]:
        print(f"  {source['display_name']}: {source['licence']}")
    return 0


def command_survey(args: argparse.Namespace) -> int:
    result = run_survey(_request_from_args(args), progress=ConsoleProgress())
    print(f"\nPackage: {result.paths.root}")
    print(f"Urbano project setting: {result.paths.project_setting.name}")
    if not result.complete:
        print("Package is INCOMPLETE. See survey.json for which tiles failed.", file=sys.stderr)
        return 1
    return 0


def command_ui(args: argparse.Namespace) -> int:
    from mapgen.web.server import serve

    serve(open_browser=not args.no_browser, port=args.port)
    return 0


def command_sources(args: argparse.Namespace) -> int:
    for source in available_sources():
        key = " (needs an API key)" if source.requires_api_key else ""
        print(f"{source.id}: {source.display_name}{key}")
        print(f"  {source.licence}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapgen",
        description="Site survey data packaging for architectural work.",
    )
    parser.add_argument("--version", action="version", version=f"mapgen {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    survey = subparsers.add_parser("survey", help="Download and package a survey area.")
    _add_survey_arguments(survey)
    survey.set_defaults(func=command_survey)

    estimate = subparsers.add_parser("estimate", help="Report tiles, size and duration only.")
    _add_survey_arguments(estimate)
    estimate.add_argument("--json", action="store_true")
    estimate.set_defaults(func=command_estimate)

    ui = subparsers.add_parser("ui", help="Open the map picker in a browser.")
    ui.add_argument("--port", type=int, default=0, help="0 picks a free port.")
    ui.add_argument("--no-browser", action="store_true")
    ui.set_defaults(func=command_ui)

    sources = subparsers.add_parser("sources", help="List available data sources.")
    sources.set_defaults(func=command_sources)

    # Legacy names, same machinery.
    for legacy in ("plan", "download", "merge", "urbano-package"):
        alias = subparsers.add_parser(legacy, help=f"Alias for survey ({legacy}).")
        _add_survey_arguments(alias)
        alias.set_defaults(
            func=command_estimate if legacy == "plan" else command_survey,
            json=False,
        )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    register_default_sources()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if argv is not None and exc.code == 2:
            print("Invalid arguments. Check the bbox format: west,south,east,north",
                  file=sys.stderr)
        raise

    try:
        return args.func(args)
    except (NamingError, UnknownSourceError, BBoxError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
```

Create `src/mapgen/__main__.py`:

```python
import sys

from mapgen.cli import main

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Handle the argparse exit path for a bad bbox**

`test_survey_reports_a_bad_bbox_without_a_traceback` expects exit code 2 and a message on stderr rather than a `SystemExit` propagating. Adjust `main` so it catches `SystemExit` from `parse_args`, prints the message, and returns the code:

```python
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        if code != 0:
            print(
                "Invalid arguments. bbox format is west,south,east,north, "
                "for example -3.29,51.38,-3.28,51.39",
                file=sys.stderr,
            )
        return code
```

Replace the earlier `try`/`except SystemExit` block with this version.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py -v`
Expected: 8 passed.

Note: `test_estimate_prints_tile_count` and friends depend on `mapgen.config`, written in the next step of this task.

- [ ] **Step 6: Add the config module the CLI imports**

Create `src/mapgen/config.py`:

```python
"""User config, remembered between runs.

Only holds preferences, never secrets. API keys come from the environment.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mapgen.fsutil import atomic_write_text

CONFIG_PATH = Path.home() / ".mapgen" / "config.json"
DEFAULT_OUTPUT_ROOT = Path.home() / "Surveys"


@dataclass
class Config:
    output_root: str = str(DEFAULT_OUTPUT_ROOT)
    tile_size_m: float = 2000.0
    overlap_m: float = 100.0
    last_region: str = ""


def load_config(path: Path | None = None) -> Config:
    target = path or CONFIG_PATH
    if not target.exists():
        return Config()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Config()
    known = {field: payload[field] for field in Config().__dict__ if field in payload}
    return Config(**known)


def save_config(config: Config, path: Path | None = None) -> None:
    target = path or CONFIG_PATH
    atomic_write_text(target, json.dumps(asdict(config), indent=2))
```

Create `tests/test_config.py`:

```python
from pathlib import Path

from mapgen.config import Config, load_config, save_config


def test_load_returns_defaults_when_absent(tmp_path):
    config = load_config(tmp_path / "absent.json")
    assert config.tile_size_m == 2000.0
    assert config.output_root


def test_save_then_load_round_trips(tmp_path):
    target = tmp_path / "config.json"
    save_config(Config(output_root="D:/Surveys", last_region="South Wales"), target)
    loaded = load_config(target)
    assert loaded.output_root == "D:/Surveys"
    assert loaded.last_region == "South Wales"


def test_a_corrupt_config_falls_back_to_defaults(tmp_path):
    target = tmp_path / "config.json"
    target.write_text("{not json", encoding="utf-8")
    assert load_config(target).tile_size_m == 2000.0


def test_unknown_keys_are_ignored(tmp_path):
    target = tmp_path / "config.json"
    target.write_text('{"output_root": "D:/S", "from_the_future": 1}', encoding="utf-8")
    assert load_config(target).output_root == "D:/S"
```

- [ ] **Step 7: Run the CLI and config tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_cli.py tests/test_config.py -v`
Expected: 12 passed.

- [ ] **Step 8: Verify the console script works**

Run: `.\.venv\Scripts\mapgen.exe --version`
Expected: prints `mapgen 1.0.0`

Run: `.\.venv\Scripts\mapgen.exe sources`
Expected: lists osm, overture and elevation with their licences.

- [ ] **Step 9: Commit**

```bash
git add src/mapgen/cli.py src/mapgen/__main__.py src/mapgen/config.py tests/test_cli.py tests/test_config.py
git commit -m "feat(cli): survey, estimate, ui and sources commands with legacy aliases"
```

---

### Task 15: web/server.py, the local API

**Files:**
- Create: `src/mapgen/web/__init__.py`
- Create: `src/mapgen/web/server.py`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: `mapgen.package`, `mapgen.config`, `mapgen.jobs`.
- Produces:
  - `JobManager` class: `start(request) -> str` returning a job id, `get(job_id) -> JobRecord`, `cancel(job_id) -> bool`, `is_busy() -> bool`. Rejects a second concurrent job with `JobBusyError`.
  - `JobRecord` dataclass: `id: str`, `state: str` in `{"running", "done", "failed", "cancelled"}`, `events: list[dict]`, `error: str | None`, `result_root: str | None`.
  - `make_handler(manager, token, static_dir)` returning a `BaseHTTPRequestHandler` subclass.
  - `serve(open_browser=True, port=0, host="127.0.0.1") -> None`.
  - `JobBusyError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_server.py`:

```python
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from mapgen.sources.base import Estimate, clear_registry, register
from mapgen.web.server import JobBusyError, JobManager, build_server

TOKEN = "test-token"


class StubSource:
    id = "stub"
    display_name = "Stub"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir):
        out = out_dir / "stub.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registry()
    register(StubSource())
    yield
    clear_registry()


@pytest.fixture
def server():
    httpd = build_server(host="127.0.0.1", port=0, token=TOKEN)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _post(base, path, payload, token=TOKEN):
    request = urllib.request.Request(
        f"{base}{path}?token={token}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get(base, path, token=TOKEN):
    with urllib.request.urlopen(f"{base}{path}?token={token}", timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_requests_without_a_token_are_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{server}/api/config", timeout=10)
    assert excinfo.value.code == 403


def test_requests_with_the_wrong_token_are_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server, "/api/config", token="wrong")
    assert excinfo.value.code == 403


def test_config_endpoint_returns_the_saved_defaults(server):
    status, payload = _get(server, "/api/config")
    assert status == 200
    assert "output_root" in payload
    assert payload["tile_size_m"] > 0


def test_sources_endpoint_lists_the_registry(server):
    status, payload = _get(server, "/api/sources")
    assert status == 200
    assert payload[0]["id"] == "stub"
    assert payload[0]["requires_api_key"] is False


def test_estimate_endpoint_returns_tile_count(server, tmp_path):
    status, payload = _post(
        server,
        "/api/estimate",
        {
            "bbox": "-3.29,51.38,-3.28,51.39",
            "region": "South Wales",
            "site": "Barry",
            "output_root": str(tmp_path),
            "tile_size_m": 600,
            "overlap_m": 50,
            "sources": ["stub"],
        },
    )
    assert status == 200
    assert payload["tiles"] >= 1


def test_estimate_returns_400_for_a_bad_bbox(server, tmp_path):
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _post(
            server,
            "/api/estimate",
            {
                "bbox": "nonsense",
                "region": "R",
                "site": "S",
                "output_root": str(tmp_path),
                "sources": ["stub"],
            },
        )
    assert excinfo.value.code == 400


def test_index_is_served_without_a_token(server):
    with urllib.request.urlopen(f"{server}/", timeout=10) as response:
        assert response.status == 200
        assert b"<html" in response.read().lower()


def test_job_manager_rejects_a_second_concurrent_job():
    manager = JobManager()
    manager._busy = True  # simulate a running job
    with pytest.raises(JobBusyError):
        manager.ensure_free()


def test_job_manager_reports_when_it_is_free():
    manager = JobManager()
    manager.ensure_free()
    assert manager.is_busy() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mapgen.web'`

- [ ] **Step 3: Write the implementation**

Create `src/mapgen/web/__init__.py` as an empty file, then create `src/mapgen/web/server.py`:

```python
"""Local HTTP server for the map picker.

Binds loopback only and requires a per-launch token, so nothing else on the
machine can drive a job. One job at a time, because concurrent Overpass jobs
from a single machine are how you get rate limited.
"""

from __future__ import annotations

import json
import secrets
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mapgen.config import Config, load_config, save_config
from mapgen.geo import BBox, BBoxError
from mapgen.jobs import CancelToken, Cancelled, EventLog
from mapgen.naming import NamingError
from mapgen.package import (
    SurveyRequest,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import available_sources

STATIC_DIR = Path(__file__).resolve().parent / "static"


class JobBusyError(RuntimeError):
    """Raised when a second job is requested while one is running."""


@dataclass
class JobRecord:
    id: str
    state: str = "running"
    events: list[dict] = field(default_factory=list)
    error: str | None = None
    result_root: str | None = None
    cancel: CancelToken = field(default_factory=CancelToken)


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._busy = False
        self._lock = threading.Lock()

    def is_busy(self) -> bool:
        return self._busy

    def ensure_free(self) -> None:
        if self._busy:
            raise JobBusyError(
                "A download is already running. Wait for it to finish or cancel it."
            )

    def start(self, request: SurveyRequest) -> str:
        with self._lock:
            self.ensure_free()
            self._busy = True

        job_id = uuid.uuid4().hex[:12]
        record = JobRecord(id=job_id)
        self._jobs[job_id] = record
        log = EventLog(listener=record.events.append)

        def worker() -> None:
            try:
                result = run_survey(request, progress=log, cancel=record.cancel)
                record.result_root = str(result.paths.root)
                record.state = "done" if result.complete else "failed"
                if not result.complete:
                    record.error = "Some tiles failed. See survey.json."
            except Cancelled:
                record.state = "cancelled"
            except Exception as exc:
                record.state = "failed"
                record.error = str(exc)
            finally:
                self._busy = False

        threading.Thread(target=worker, daemon=True).start()
        return job_id

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        record = self._jobs.get(job_id)
        if record is None:
            return False
        record.cancel.cancel()
        return True


def _survey_request(payload: dict) -> SurveyRequest:
    return SurveyRequest(
        bbox=BBox.parse(payload["bbox"]),
        region=payload["region"],
        site=payload["site"],
        output_root=Path(payload["output_root"]),
        tile_size_m=float(payload.get("tile_size_m", 2000.0)),
        overlap_m=float(payload.get("overlap_m", 100.0)),
        source_ids=tuple(payload.get("sources") or ("osm", "overture")),
        keep_work=bool(payload.get("keep_work", False)),
        coordinate_stem=bool(payload.get("coordinate_stem", False)),
        run_bridge_step=bool(payload.get("run_bridge", True)),
    )


def make_handler(manager: JobManager, token: str, static_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # keep the console clean
            return

        # --- helpers -------------------------------------------------
        def _send_json(self, status: int, payload: object) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self, query: dict) -> bool:
            return query.get("token", [None])[0] == token

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        # --- routes --------------------------------------------------
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)

            if not parsed.path.startswith("/api/"):
                return self._serve_static(parsed.path)

            if not self._authorised(query):
                return self._send_json(403, {"error": "Invalid or missing token."})

            if parsed.path == "/api/config":
                config = load_config()
                return self._send_json(200, config.__dict__)

            if parsed.path == "/api/sources":
                return self._send_json(
                    200,
                    [
                        {
                            "id": s.id,
                            "display_name": s.display_name,
                            "licence": s.licence,
                            "requires_api_key": s.requires_api_key,
                        }
                        for s in available_sources()
                    ],
                )

            if parsed.path.startswith("/api/jobs/"):
                job_id = parsed.path.split("/")[3]
                record = manager.get(job_id)
                if record is None:
                    return self._send_json(404, {"error": "Unknown job."})
                return self._send_json(
                    200,
                    {
                        "id": record.id,
                        "state": record.state,
                        "events": record.events,
                        "error": record.error,
                        "result_root": record.result_root,
                    },
                )

            return self._send_json(404, {"error": "Unknown endpoint."})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._authorised(query):
                return self._send_json(403, {"error": "Invalid or missing token."})

            try:
                payload = self._read_json()
            except json.JSONDecodeError:
                return self._send_json(400, {"error": "Body was not valid JSON."})

            if parsed.path == "/api/estimate":
                try:
                    return self._send_json(200, estimate_survey(_survey_request(payload)))
                except (BBoxError, NamingError, KeyError) as exc:
                    return self._send_json(400, {"error": str(exc)})

            if parsed.path == "/api/jobs":
                try:
                    job_id = manager.start(_survey_request(payload))
                except JobBusyError as exc:
                    return self._send_json(409, {"error": str(exc)})
                except (BBoxError, NamingError, KeyError) as exc:
                    return self._send_json(400, {"error": str(exc)})
                return self._send_json(202, {"id": job_id})

            if parsed.path.endswith("/cancel"):
                job_id = parsed.path.split("/")[3]
                if not manager.cancel(job_id):
                    return self._send_json(404, {"error": "Unknown job."})
                return self._send_json(200, {"cancelled": True})

            return self._send_json(404, {"error": "Unknown endpoint."})

        def do_PUT(self) -> None:
            parsed = urlparse(self.path)
            if not self._authorised(parse_qs(parsed.query)):
                return self._send_json(403, {"error": "Invalid or missing token."})
            if parsed.path != "/api/config":
                return self._send_json(404, {"error": "Unknown endpoint."})
            payload = self._read_json()
            current = load_config()
            for key, value in payload.items():
                if hasattr(current, key):
                    setattr(current, key, value)
            save_config(current)
            return self._send_json(200, current.__dict__)

        def _serve_static(self, path: str) -> None:
            relative = "index.html" if path in ("/", "") else path.lstrip("/")
            target = (static_dir / relative).resolve()
            if not str(target).startswith(str(static_dir.resolve())) or not target.is_file():
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            content_types = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }
            body = target.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", content_types.get(target.suffix, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def build_server(host: str = "127.0.0.1", port: int = 0, token: str | None = None):
    register_default_sources()
    resolved_token = token or secrets.token_urlsafe(24)
    manager = JobManager()
    handler = make_handler(manager, resolved_token, STATIC_DIR)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.mapgen_token = resolved_token
    return httpd


def serve(open_browser: bool = True, port: int = 0, host: str = "127.0.0.1") -> None:
    httpd = build_server(host=host, port=port)
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/?token={httpd.mapgen_token}"
    print(f"mapgen UI: {url}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
```

- [ ] **Step 4: Create a placeholder index so the static test passes**

Create `src/mapgen/web/static/index.html` with minimal valid content. Task 16 replaces it entirely.

```html
<!doctype html>
<html lang="en">
  <head><meta charset="utf-8" /><title>mapgen</title></head>
  <body><p>The interface is built in the next task.</p></body>
</html>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_web_server.py -v`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add src/mapgen/web/__init__.py src/mapgen/web/server.py src/mapgen/web/static/index.html tests/test_web_server.py
git commit -m "feat(web): loopback API with token auth, job manager, one job at a time"
```

---

### Task 16: the map picker interface

**Files:**
- Create: `src/mapgen/web/static/vendor/leaflet.css`
- Create: `src/mapgen/web/static/vendor/leaflet.js`
- Modify: `src/mapgen/web/static/index.html` (replacing the placeholder from Task 15)
- Create: `src/mapgen/web/static/styles.css`
- Create: `src/mapgen/web/static/app.js`

**Interfaces:**
- Consumes: the API from Task 15: `GET /api/config`, `GET /api/sources`, `POST /api/estimate`, `POST /api/jobs`, `GET /api/jobs/<id>`, `POST /api/jobs/<id>/cancel`, `PUT /api/config`.
- Produces: no Python interfaces. This task is verified by hand, not by unit test.

- [ ] **Step 1: Vendor Leaflet**

Leaflet is fetched once and committed, so the interface never depends on a CDN being up.

```powershell
$vendor = "src\mapgen\web\static\vendor"
New-Item -ItemType Directory -Force $vendor | Out-Null
Invoke-WebRequest -Uri "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" -OutFile "$vendor\leaflet.js"
Invoke-WebRequest -Uri "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" -OutFile "$vendor\leaflet.css"
Get-FileHash "$vendor\leaflet.js" -Algorithm SHA256
```

Record the hash in the commit body. Leaflet's marker icon PNGs are not needed because this interface draws only a rectangle.

Add an exception to `.gitignore` so the vendored files are tracked despite any broad rules:

```gitignore
!src/mapgen/web/static/vendor/*
```

- [ ] **Step 2: Write the markup**

Replace `src/mapgen/web/static/index.html`:

```html
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>mapgen</title>
    <link rel="stylesheet" href="vendor/leaflet.css" />
    <link rel="stylesheet" href="styles.css" />
  </head>
  <body>
    <header>
      <h1>mapgen</h1>
      <p class="tagline">Draw an extent, name it, get a folder.</p>
    </header>

    <main>
      <section class="map-pane">
        <div id="map"></div>
        <div class="map-tools">
          <button id="draw" type="button">Draw extent</button>
          <input id="place" type="search" placeholder="Search a place name" />
          <input id="bbox" type="text" placeholder="or paste west,south,east,north" />
        </div>
      </section>

      <section class="form-pane">
        <label>Region
          <input id="region" type="text" placeholder="South Wales" />
        </label>
        <label>Site
          <input id="site" type="text" placeholder="Barry Waterfront" />
        </label>

        <div class="row">
          <label>Tile size (m)
            <input id="tile-size" type="number" min="500" step="100" value="2000" />
          </label>
          <label>Overlap (m)
            <input id="overlap" type="number" min="0" step="10" value="100" />
          </label>
        </div>

        <fieldset>
          <legend>Layers</legend>
          <div id="sources"></div>
        </fieldset>

        <label>Output root
          <input id="output-root" type="text" />
        </label>

        <label class="checkbox">
          <input id="keep-work" type="checkbox" />
          Keep intermediate tiles
        </label>

        <div id="estimate" class="estimate">Draw or paste an extent to see an estimate.</div>

        <button id="download" type="button" class="primary" disabled>Download package</button>
        <button id="cancel" type="button" class="danger" hidden>Cancel</button>
      </section>
    </main>

    <footer>
      <div id="log" class="log"></div>
    </footer>

    <script src="vendor/leaflet.js"></script>
    <script src="app.js"></script>
  </body>
</html>
```

- [ ] **Step 3: Write the styles**

Create `src/mapgen/web/static/styles.css`:

```css
:root {
  --ink: #1c1c1a;
  --paper: #f6f4ef;
  --line: #cfcabd;
  --accent: #2f5d4f;
  --warn: #8c3b2e;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  font: 15px/1.5 "Segoe UI", system-ui, sans-serif;
  color: var(--ink);
  background: var(--paper);
  display: flex;
  flex-direction: column;
  height: 100vh;
}

header {
  padding: 12px 20px;
  border-bottom: 1px solid var(--line);
}

header h1 { margin: 0; font-size: 18px; letter-spacing: 0.04em; }
.tagline { margin: 2px 0 0; font-size: 13px; opacity: 0.7; }

main {
  flex: 1;
  display: grid;
  grid-template-columns: 1fr 340px;
  min-height: 0;
}

.map-pane { position: relative; display: flex; flex-direction: column; min-height: 0; }
#map { flex: 1; min-height: 0; }

.map-tools {
  display: flex;
  gap: 8px;
  padding: 10px;
  border-top: 1px solid var(--line);
  background: var(--paper);
}

.map-tools input { flex: 1; }

.form-pane {
  border-left: 1px solid var(--line);
  padding: 16px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
label.checkbox { flex-direction: row; align-items: center; gap: 8px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }

input[type="text"], input[type="number"], input[type="search"] {
  padding: 7px 9px;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: #fff;
  font: inherit;
}

fieldset { border: 1px solid var(--line); border-radius: 3px; padding: 10px; margin: 0; }
legend { font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.7; }
#sources label { flex-direction: row; align-items: center; gap: 8px; padding: 3px 0; }
#sources input[disabled] + span { opacity: 0.5; }

.estimate {
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  padding: 10px;
  font-size: 13px;
  background: #fff;
}

.estimate.error { border-left-color: var(--warn); color: var(--warn); }

button {
  padding: 9px 12px;
  border: 1px solid var(--line);
  border-radius: 3px;
  background: #fff;
  font: inherit;
  cursor: pointer;
}

button.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
button.primary:disabled { opacity: 0.4; cursor: not-allowed; }
button.danger { background: var(--warn); color: #fff; border-color: var(--warn); }

footer { border-top: 1px solid var(--line); }

.log {
  height: 120px;
  overflow-y: auto;
  padding: 8px 20px;
  font: 12px/1.5 Consolas, monospace;
  background: #fff;
}

.log .fail { color: var(--warn); }
```

- [ ] **Step 4: Write the behaviour**

Create `src/mapgen/web/static/app.js`:

```javascript
const token = new URLSearchParams(location.search).get("token") || "";
const $ = (id) => document.getElementById(id);

let bbox = null;
let rectangle = null;
let drawing = false;
let jobId = null;
let poller = null;

const map = L.map("map").setView([51.48, -3.18], 11);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
}).addTo(map);

// --- API -------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(`${path}?token=${encodeURIComponent(token)}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
  return payload;
}

// --- extent selection ------------------------------------------------

function setBBox(next, fit = true) {
  bbox = next;
  if (rectangle) map.removeLayer(rectangle);
  const bounds = [
    [bbox.south, bbox.west],
    [bbox.north, bbox.east],
  ];
  rectangle = L.rectangle(bounds, { color: "#2f5d4f", weight: 2, fillOpacity: 0.08 });
  rectangle.addTo(map);
  if (fit) map.fitBounds(bounds);
  $("bbox").value = `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`;
  suggestNames();
  refreshEstimate();
}

$("draw").addEventListener("click", () => {
  drawing = true;
  $("draw").textContent = "Click two corners";
  map.getContainer().style.cursor = "crosshair";
});

let firstCorner = null;
map.on("click", (event) => {
  if (!drawing) return;
  if (!firstCorner) {
    firstCorner = event.latlng;
    return;
  }
  const a = firstCorner;
  const b = event.latlng;
  setBBox(
    {
      west: Math.min(a.lng, b.lng),
      south: Math.min(a.lat, b.lat),
      east: Math.max(a.lng, b.lng),
      north: Math.max(a.lat, b.lat),
    },
    false
  );
  firstCorner = null;
  drawing = false;
  $("draw").textContent = "Draw extent";
  map.getContainer().style.cursor = "";
});

$("bbox").addEventListener("change", () => {
  const parts = $("bbox").value.split(",").map((v) => parseFloat(v.trim()));
  if (parts.length !== 4 || parts.some(Number.isNaN)) {
    showEstimateError("Paste four numbers: west,south,east,north");
    return;
  }
  const [w, s, e, n] = parts;
  setBBox({
    west: Math.min(w, e),
    south: Math.min(s, n),
    east: Math.max(w, e),
    north: Math.max(s, n),
  });
});

$("place").addEventListener("change", async () => {
  const query = $("place").value.trim();
  if (!query) return;
  const url = `https://nominatim.openstreetmap.org/search?format=json&limit=1&q=${encodeURIComponent(query)}`;
  const [hit] = await fetch(url).then((r) => r.json());
  if (!hit) return showEstimateError(`No match for "${query}"`);
  const [south, north, west, east] = hit.boundingbox.map(parseFloat);
  setBBox({ west, south, east, north });
});

// Auto-suggest region and site from the box centre. Never overwrites typing.
async function suggestNames() {
  if (!bbox) return;
  if ($("region").value && $("site").value) return;
  const lat = (bbox.south + bbox.north) / 2;
  const lon = (bbox.west + bbox.east) / 2;
  try {
    const url = `https://nominatim.openstreetmap.org/reverse?format=json&zoom=12&lat=${lat}&lon=${lon}`;
    const data = await fetch(url).then((r) => r.json());
    const a = data.address || {};
    if (!$("site").value) $("site").value = a.suburb || a.town || a.village || a.city || "";
    if (!$("region").value) $("region").value = a.county || a.state_district || a.state || "";
  } catch (error) {
    // A failed geocode leaves the fields blank for the user to fill. Not an error.
  }
}

// --- estimate --------------------------------------------------------

function payload() {
  return {
    bbox: `${bbox.west},${bbox.south},${bbox.east},${bbox.north}`,
    region: $("region").value.trim(),
    site: $("site").value.trim(),
    output_root: $("output-root").value.trim(),
    tile_size_m: parseFloat($("tile-size").value),
    overlap_m: parseFloat($("overlap").value),
    keep_work: $("keep-work").checked,
    sources: [...document.querySelectorAll("#sources input:checked")].map((i) => i.value),
  };
}

function showEstimateError(message) {
  const box = $("estimate");
  box.className = "estimate error";
  box.textContent = message;
  $("download").disabled = true;
}

async function refreshEstimate() {
  if (!bbox || !$("region").value.trim() || !$("site").value.trim()) {
    $("estimate").className = "estimate";
    $("estimate").textContent = "Enter a region and site to see an estimate.";
    $("download").disabled = true;
    return;
  }
  try {
    const data = await api("/api/estimate", {
      method: "POST",
      body: JSON.stringify(payload()),
    });
    const minutes = Math.max(1, Math.round(data.seconds_estimate / 60));
    $("estimate").className = "estimate";
    $("estimate").innerHTML =
      `<strong>${data.extent_km.width.toFixed(2)} x ${data.extent_km.height.toFixed(2)} km</strong><br />` +
      `${data.tiles} tiles (${data.rows} x ${data.cols})<br />` +
      `around ${Math.round(data.bytes_estimate / 1e6)} MB, about ${minutes} min`;
    $("download").disabled = false;
  } catch (error) {
    showEstimateError(error.message);
  }
}

["region", "site", "tile-size", "overlap", "output-root"].forEach((id) =>
  $(id).addEventListener("change", refreshEstimate)
);

// --- job -------------------------------------------------------------

function log(message, failed = false) {
  const line = document.createElement("div");
  if (failed) line.className = "fail";
  line.textContent = message;
  $("log").appendChild(line);
  $("log").scrollTop = $("log").scrollHeight;
}

$("download").addEventListener("click", async () => {
  $("log").innerHTML = "";
  try {
    const started = await api("/api/jobs", {
      method: "POST",
      body: JSON.stringify(payload()),
    });
    jobId = started.id;
    $("download").disabled = true;
    $("cancel").hidden = false;
    let seen = 0;
    poller = setInterval(async () => {
      const job = await api(`/api/jobs/${jobId}`);
      job.events.slice(seen).forEach((e) => {
        const detail = Object.entries(e)
          .filter(([k]) => k !== "event")
          .map(([k, v]) => `${k}=${v}`)
          .join(" ");
        log(`${e.event} ${detail}`.trim());
      });
      seen = job.events.length;
      if (job.state !== "running") {
        clearInterval(poller);
        $("cancel").hidden = true;
        $("download").disabled = false;
        if (job.state === "done") log(`Finished: ${job.result_root}`);
        else log(`${job.state}: ${job.error || ""}`, true);
      }
    }, 700);
  } catch (error) {
    showEstimateError(error.message);
  }
});

$("cancel").addEventListener("click", async () => {
  if (jobId) await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
});

// --- boot ------------------------------------------------------------

(async function boot() {
  const config = await api("/api/config");
  $("output-root").value = config.output_root;
  $("tile-size").value = config.tile_size_m;
  $("overlap").value = config.overlap_m;
  if (config.last_region) $("region").value = config.last_region;

  const sources = await api("/api/sources");
  $("sources").innerHTML = sources
    .map(
      (s) => `
      <label title="${s.licence}">
        <input type="checkbox" value="${s.id}" ${s.id === "elevation" ? "" : "checked"} />
        <span>${s.display_name}${s.requires_api_key ? " (needs an API key)" : ""}</span>
      </label>`
    )
    .join("");
  $("sources").addEventListener("change", refreshEstimate);
})();
```

- [ ] **Step 5: MANUAL VERIFICATION, drive the interface**

Run: `.\.venv\Scripts\mapgen.exe ui`

Confirm each of the following, and note any that fail:

1. The browser opens and the map renders. A missing map means the vendored Leaflet did not land.
2. **Draw extent** then two clicks produces a rectangle, and the bbox field fills in.
3. Pasting `-3.29,51.38,-3.28,51.39` into the bbox field draws the same rectangle.
4. Searching `Barry, Wales` moves the map and sets the extent.
5. Region and site auto-fill after an extent is set, and typing over them is not clobbered by a later geocode.
6. The estimate panel shows extent, tile count, size and duration.
7. Setting the output root to `C:\` plus 200 characters makes the estimate panel show the path length error rather than letting the download start.
8. A small download runs, log lines appear, and the finished path is printed.
9. **Cancel** during a run stops it and the log says cancelled.
10. Starting a second download while one runs is refused with a clear message.

- [ ] **Step 6: Commit**

```bash
git add src/mapgen/web/static .gitignore
git commit -m "feat(web): map picker interface with live estimate and job log"
```

---

### Task 17: README, legacy removal, and end-to-end verification

**Files:**
- Create: `README.md`
- Delete: `osm_overture_tiles.py`
- Delete: `osm-overture-tiling.md`
- Create: `tests/test_live_smoke.py`

**Interfaces:**
- Consumes: everything.
- Produces: a repository that a stranger can clone and run.

- [ ] **Step 1: Verify parity with the old script before deleting it**

The old script is the only reference for correct output. Run a real survey through the new tool over a small Welsh extent:

```powershell
.\.venv\Scripts\mapgen.exe survey `
  --bbox=-3.29,51.38,-3.28,51.39 `
  --region "South Wales" `
  --site "Barry Waterfront" `
  --tile-size-m 1000 `
  --overlap-m 50 `
  --source osm --source overture `
  --overture-type building --overture-type water
```

Confirm the produced folder contains a merged `.osm`, per-type GeoJSON, `layers/water.geojson`, `survey.json` with `"complete": true`, and the Urbano `_project_setting.json`. Open the merged OSM in Rhino or QGIS and confirm the geometry lands in the right place. Compare feature counts against `south-wales-package-4` for a sanity check on magnitude.

If anything is wrong, stop and fix it before deleting the old script.

- [ ] **Step 2: Add the marked live smoke test**

Create `tests/test_live_smoke.py`:

```python
"""Excluded from the default run. Invoke with: pytest -m live"""

import pytest

from mapgen.geo import BBox
from mapgen.package import SurveyRequest, register_default_sources, run_survey


@pytest.mark.live
def test_a_small_welsh_extent_downloads_end_to_end(tmp_path):
    register_default_sources()
    request = SurveyRequest(
        bbox=BBox.parse("-3.29,51.38,-3.285,51.385"),
        region="South Wales",
        site="Smoke Test",
        output_root=tmp_path,
        tile_size_m=1000.0,
        overlap_m=50.0,
        source_ids=("osm",),
        run_bridge_step=False,
    )
    result = run_survey(request)
    assert result.complete is True
    assert (result.paths.root / "all.osm").stat().st_size > 0
    assert result.paths.survey_json.exists()
```

- [ ] **Step 3: Confirm the live test passes**

Run: `.\.venv\Scripts\python.exe -m pytest -m live -v`
Expected: 1 passed. This is the only test that touches the network.

- [ ] **Step 4: Write the README**

Create `README.md`:

````markdown
# mapgen

A site survey data tool for architectural work. Draw an extent, name it, get a
folder that Grasshopper and Urbano 2 can read.

## What it does

Downloads OpenStreetMap, Overture Maps and elevation data for an area you draw
on a map, deduplicates the overlapping tiles, and writes a dated and named
package with an Urbano 2 project setting file and a full audit trail of where
every layer came from and under what licence.

## Setup

Requires Python 3.11 or newer and the .NET SDK for the Urbano bridge.

```powershell
.\bootstrap.ps1
.\.venv\Scripts\Activate.ps1
```

For elevation, get a free key from portal.opentopography.org and set it:

```powershell
$env:OPENTOPOGRAPHY_API_KEY = "your-key"
```

## Use

```powershell
mapgen ui
```

Draw a box, name the region and site, press Download package.

From the command line:

```powershell
mapgen survey --bbox=-3.29,51.38,-3.28,51.39 --region "South Wales" --site "Barry Waterfront"
mapgen estimate --bbox=-3.29,51.38,-3.28,51.39 --region "South Wales" --site "Barry" 
mapgen sources
```

## What you get

```text
Surveys/South-Wales/2026-08-01_Barry-Waterfront/
  Barry-Waterfront_2026-08-01.osm.pbf
  Barry-Waterfront_2026-08-01.egrid
  Barry-Waterfront_2026-08-01_overture.parquet
  Barry-Waterfront_2026-08-01_project_setting.json   <- point Urbano here
  survey.json                                        <- bbox, licences, per-tile log
  layers/
    water.geojson
    vegetation.geojson
    landuse.geojson
```

## Notes

- Overpass and the OSM API are free public services. Requests are rate limited
  and backed off deliberately. Large areas take a while.
- The OSM map API caps a request at 50000 nodes. In dense areas reduce the tile
  size to 1500 or 2000 metres.
- A job that fails partway keeps its `_work` folder. Re-running the same survey
  resumes rather than restarting.
- Windows resolves paths at 260 characters. mapgen refuses a job that would
  exceed 240 before downloading anything, rather than dying halfway.

## Data sources and licences

| Source | Licence |
| --- | --- |
| OpenStreetMap | Open Database License (ODbL) 1.0 |
| Overture Maps | ODbL and CDLA-Permissive-2.0, mixed by theme |
| Copernicus DEM via OpenTopography | Free for any use with attribution |

Attribution strings for every layer in a package are recorded in its
`survey.json`.

## Roadmap

Phase 2 adds UK survey-grade layers behind the same `LayerSource` interface:
NRW LiDAR at 1 m via DataMapWales (Open Government Licence, and a far better
elevation source than COP30 anywhere in Wales), OS NGD buildings and water
network, and open drainage data. See `docs/superpowers/specs/`.
````

- [ ] **Step 5: Delete the superseded script and its notes**

Every function has moved into `src/mapgen/`, with tests. Git retains the history.

```bash
git rm osm_overture_tiles.py osm-overture-tiling.md
```

- [ ] **Step 6: Confirm nothing referenced the deleted files**

Run: `git grep -n "osm_overture_tiles\|osm-overture-tiling"`
Expected: matches only inside `docs/superpowers/`, which are historical design documents and should keep their references. If anything in `src/` or `tests/` matches, fix it before committing.

- [ ] **Step 7: Run the full suite one final time**

Run: `.\.venv\Scripts\python.exe -m pytest -v`
Expected: every test passes, and the live test is not among them.

Then confirm the live test still runs when asked:

Run: `.\.venv\Scripts\python.exe -m pytest -m live -v`
Expected: 1 passed.

- [ ] **Step 8: Confirm the repository is clean enough to publish**

Run: `git status --short --untracked-files=all`
Expected: no survey outputs, no `.venv`, no `bin` or `obj`, no `temp_*` files. If any appear, extend `.gitignore` rather than deleting the files.

Run: `du -sh .git`
Expected: still small, a few hundred KB. A large `.git` means binary data was committed at some point and needs removing before pushing.

- [ ] **Step 9: Commit**

```bash
git add README.md tests/test_live_smoke.py
git commit -m "docs: README, live smoke test, and removal of the superseded script"
```

- [ ] **Step 10: Clean up the working folder before publishing**

The folder still holds the artefacts the spec flagged. They are gitignored, so this is housekeeping rather than a repository concern, and it is the user's call whether to keep them:

- `temp_r00_c04_overpass.osm` (200 MB, orphaned scratch)
- `temp_test_place.geojson`
- `data/`, `south-wales-package-2/`, `south-wales-package-4/`

Ask before deleting any of them. `south-wales-package-4` in particular is the reference output used for parity checking and is worth keeping until phase 2 is done.

---

## Verification summary

Manual gates in this plan, in the order they occur:

| Task | Gate | If it fails |
| --- | --- | --- |
| 2, step 9 | Urbano 2 loads a package with a readable file stem | Stop. Revert to the coordinate stem, amend Task 4, continue |
| 2, step 10 | Urbano's `Layers` array, consumed or ignored | Neither outcome blocks. Record it for Task 13 |
| 16, step 5 | The interface drives a real download end to end | Fix before Task 17 |
| 17, step 1 | Output parity against the old script | Fix before deleting the old script |
