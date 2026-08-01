# mapgen: design

A site survey data tool for architectural work. Draw an extent, name it, get a
folder Grasshopper and Urbano 2 can read.

Date: 2026-08-01
Status: approved, ready for implementation planning
Phase: 1 of 2

## Problem

`osm_overture_tiles.py` already downloads tiled OSM and Overture data for an
arbitrary bounding box and packages it for Urbano 2. It works, and the South
Wales package in `south-wales-package-4/` proves the non-US path runs end to
end. What it is not is a tool. Using it means composing multi-line PowerShell
invocations with hand-normalised bbox strings, inventing a folder name each
time, and reading coordinate-encoded filenames to work out which package is
which. Failures halfway through a long job mean starting again.

This phase turns it into something operable: a small local UI, a naming
convention that survives contact with a project archive, and the robustness
fixes needed before survey outputs inform real project work.

## Goals

1. Draw or type an extent, name it, press a button, get a folder.
2. Folders and files named by date and location, legible a year later.
3. A single file inside each package that Urbano 2 reads without ambiguity.
4. Survive partial failure: resume rather than restart.
5. Leave a clean seam for phase 2 survey layers.
6. Be safe to publish as a public GitHub repository.

## Non-goals for this phase

- New data sources. NRW LiDAR, OS NGD, sewer catchments and storm overflows are
  phase 2. This phase builds the socket they plug into, nothing more.
- Replacing the C# `UrbanoBridge`. It stays, with one small argument added.
- Multi-user or hosted operation. This is a single-user localhost tool.
- Changing the tiling mathematics. The local-planar projection in `build_tiles`
  is accurate at the latitudes and areas this tool is used for. It degenerates
  near the poles and does not handle a bbox crossing the antimeridian, but
  neither case is worth solving now. Tests record the current behaviour rather
  than assert a fix.

## Decisions

| Decision | Choice | Why |
| --- | --- | --- |
| Scope | Phase 1 is UI, naming, robustness, Urbano handoff. Data sources are phase 2 | Four subsystems in one spec produces a plan nobody finishes |
| UI form | Local web UI with map picker | Survey work needs to see the extent before committing to a long job; four typed decimal degrees is where the errors are |
| Folder scheme | `<root>/<Region>/<YYYY-MM-DD>_<Site>/` | Groups geographically, sorts chronologically, stays short enough for Windows path limits |
| File stem | `<Site>_<YYYY-MM-DD>` | Readable at a glance; coordinates preserved inside `project_setting.json` and `survey.json` so nothing is lost |
| Extra layers | GeoJSON in `layers/`, plus a probe of Urbano's `Layers` array | Works regardless of what Urbano supports; the probe is upside, not a dependency |
| Web stack | Stdlib `http.server`, vendored Leaflet, no framework | Every dependency is a future breakage on a tool that must just work |

### Site and region naming

Both are auto-suggested by reverse-geocoding the box centre through Nominatim,
and both remain freely editable in the UI. The suggestion is a convenience, not
a constraint. If the geocode fails or times out the fields are simply left blank
for the user to fill, and the job proceeds.

### Output root

Configurable, remembered between runs in a small user config file, and
defaulting to a location outside the repository. Survey outputs are large
binary artefacts and must never be candidates for version control.

## Architecture

The existing 1,523-line module does tiling, HTTP, XML merging, DEM fetching,
subprocess orchestration and CLI parsing in one file, so every change carries
the risk surface of all of them. Splitting it is what makes phase 2 additive.

```text
mapgen/                           git repo root
  pyproject.toml                  pinned dependencies, console script entry point
  bootstrap.ps1                   creates .venv and installs, one double-click
  .gitignore
  README.md
  src/mapgen/
    __init__.py
    geo.py          bbox parse and normalise, tiling maths, projection helpers
    naming.py       slugify, folder and stem construction, path length guard
    sources/
      base.py       LayerSource protocol, the phase 2 seam
      osm.py        Overpass and OSM API fetch, retry, backoff, rate limiting
      overture.py   Overture tile fetch
      elevation.py  OpenTopography DEM
    merge.py        OSM XML dedupe and merge, GeoJSON merge
    jobs.py         job state, progress events, cancellation, resume
    package.py      orchestration: plan, download, merge, bridge, survey.json
    bridge.py       subprocess wrapper around UrbanoBridge
    web/
      server.py     threaded stdlib HTTP server, JSON API and SSE progress
      static/       index.html, app.js, styles.css, vendored leaflet
    cli.py          argparse; existing subcommands keep working unchanged
  tools/UrbanoBridge/   C#, one argument added
  tests/
  docs/superpowers/specs/
```

Migration is a move, not a rewrite. Existing functions transfer into the module
that owns their responsibility, keeping behaviour identical, with tests written
against them as they land.

### The LayerSource contract

```python
class LayerSource(Protocol):
    id: str                    # "osm", "overture", "nrw_lidar"
    display_name: str          # shown in the UI checkbox list
    licence: str               # recorded verbatim in survey.json
    attribution: str           # recorded verbatim in survey.json
    requires_api_key: bool

    def estimate(self, bbox: BBox, tiles: list[Tile]) -> Estimate: ...
    def fetch(self, bbox: BBox, tiles: list[Tile], work_dir: Path,
              progress: ProgressSink) -> list[Path]: ...
    def merge(self, parts: list[Path], out_dir: Path) -> list[Path]: ...
```

`package.py` iterates whatever sources the UI selected and knows nothing about
any specific one. A phase 2 source is one new file in `sources/` plus one
registry entry. Sources requiring an API key declare it, and the UI disables
them with an explanatory note when the key is absent, rather than failing at
hour two of a download.

## The UI

Launched by `mapgen ui`, or `python -m mapgen ui` before the console script is
on PATH, or from a desktop shortcut. The server binds
127.0.0.1 on an ephemeral port, mints a per-launch token, and opens the browser
at a URL carrying that token. Requests without the token are rejected. Loopback
binding plus the token means nothing else on the machine can drive it.

Layout is two columns over a progress strip:

- Left: a Leaflet map with a draw-rectangle control. Alternatives for setting
  the extent are pasting a bbox string and searching a place name. The current
  extent is always shown as a rectangle, whichever way it was set.
- Right: region and site fields, tile size and overlap (auto-defaulted from the
  drawn area, overridable), a layer checkbox list built from the source
  registry, and the output root with a browse control.
- Above the download button: extent in kilometres, tile count, estimated
  download size and estimated duration. This is the guard against a mis-drawn
  box costing three hours.
- Below: a live progress log, one line per tile, streamed over Server-Sent
  Events, with a cancel button that stops cleanly at the next tile boundary.

The API surface is deliberately small: `POST /api/estimate`, `POST /api/jobs`,
`GET /api/jobs/<id>/events` (SSE), `POST /api/jobs/<id>/cancel`,
`GET /api/config` and `PUT /api/config`.

Jobs run on a worker thread. The server holds one job at a time and rejects a
second with a clear message, because concurrent Overpass jobs from one machine
are how you get rate limited.

## Naming and the output contract

```text
<output root>/South-Wales/2026-08-01_Barry-Waterfront/
  Barry-Waterfront_2026-08-01.osm.pbf
  Barry-Waterfront_2026-08-01.egrid
  Barry-Waterfront_2026-08-01_overture.parquet
  Barry-Waterfront_2026-08-01_project_setting.json   <- Urbano reads this
  survey.json
  layers/
    water.geojson
    vegetation.geojson
    landuse.geojson
  _work/                                             <- removed unless kept
```

`layers/` is populated in phase 1 from Overture types the script already
downloads: `water` becomes `water.geojson`, `land_cover` becomes
`vegetation.geojson`, `land_use` becomes `landuse.geojson`. No new data source
is involved. Phase 1 changes where these merged outputs land and what they are
called, so that they are usable in Grasshopper without digging through
`merged/overture/`. Phase 2 adds files to the same folder from new sources.

`_work/` is deleted after a successful run unless the user ticks keep
intermediates in the UI, which maps to the existing `--keep-tilework` behaviour.
It is always retained after a failure, because it is what makes resume possible.

Slug rules: transliterate to ASCII, spaces to hyphens, drop characters outside
`[A-Za-z0-9-]`, collapse repeated hyphens, trim leading and trailing hyphens,
cap each component at 40 characters. An empty result after slugification is
rejected with a message naming the offending field.

Collisions append `_02`, `_03` and so on to the dated folder name. Two surveys
of the same site on the same day are a normal occurrence, not an error.

### Path length guard

Windows resolves at 260 characters by default and the OneDrive root here is
already around 110. The guard runs at plan time, before any network call. It
constructs the worst-case path the job will produce:

```text
<output root>/<Region>/<YYYY-MM-DD>_<Site>/_work/raw/overture/<longest type>/rNN_cNN.geojson
```

If that exceeds 240 characters the job is refused with a message stating the
computed length and suggesting a shorter output root. Failing before the
download is the entire point; the current code would discover this partway
through.

### The bridge change

[tools/UrbanoBridge/Program.cs:55](../../../tools/UrbanoBridge/Program.cs#L55)
currently reads:

```csharp
var fileNameStr = BuildFileNameString(options.BBox);
```

It becomes:

```csharp
var fileNameStr = options.FileNameStem ?? BuildFileNameString(options.BBox);
```

with a matching `--file-name-stem` option. `BuildFileNameString` stays as the
fallback. Everything else in the bridge is untouched: the bbox already travels
separately as `Top`, `Bottom`, `Left`, `Right` and `CoordinateReference`, and
all four data paths are already written as explicit absolute paths, so nothing
in the bridge parses coordinates back out of the stem.

A `--coordinate-stem` flag on the Python side forces the old behaviour, so if
Urbano 2 turns out to require the coordinate form the tool still works while we
adjust.

### survey.json

The audit trail. If these outputs inform project work, being able to say where
each polygon came from and under what licence is not optional.

```json
{
  "schema_version": 1,
  "tool_version": "1.0.0",
  "site": "Barry Waterfront",
  "region": "South Wales",
  "slug": { "site": "Barry-Waterfront", "region": "South-Wales" },
  "date": "2026-08-01",
  "urbano_stem": "Barry-Waterfront_2026-08-01",
  "bbox": { "west": -3.29, "south": 51.38, "east": -3.24, "north": 51.41 },
  "extent_km": { "width": 3.47, "height": 3.34 },
  "tiling": { "tile_size_m": 2000, "overlap_m": 100, "rows": 2, "cols": 2 },
  "sources": [
    {
      "id": "osm",
      "licence": "Open Database License (ODbL) 1.0",
      "attribution": "© OpenStreetMap contributors",
      "endpoints_used": ["https://overpass-api.de/api/interpreter"],
      "outputs": ["Barry-Waterfront_2026-08-01.osm.pbf"]
    }
  ],
  "tiles": [
    { "tile_id": "r00_c00", "osm": "ok", "overture": "ok" }
  ],
  "complete": true,
  "started_at": "2026-08-01T09:14:22Z",
  "finished_at": "2026-08-01T09:41:07Z"
}
```

`complete` is false whenever any tile failed, and stays false in the written
file. A package that is silently partial is worse than one that is loudly
incomplete.

## Stability fixes

Each item below is a defect observed in the current code or working folder.

1. **Scratch file leakage.** `temp_r00_c04_overpass.osm`, 200 MB, is sitting in
   the repository root right now, and `temp_test_place.geojson` beside it. All
   scratch moves inside the job's `_work/` directory, managed by a context
   manager that cleans on both the success and the failure path.
2. **No dependency pinning.** `overturemaps` is not importable from the system
   Python, so the Overture path is silently relying on a CLI fallback.
   `pyproject.toml` pins `requests` and `overturemaps`; `bootstrap.ps1` creates
   `.venv` and installs them.
3. **No resume.** Job state is written to `_work/state.json` after each tile
   completes. Re-running the same survey skips tiles already downloaded. Today
   this is only achievable by driving `--tile-id` by hand.
4. **Overpass politeness.** Keep the existing retry and backoff, add explicit
   rate limiting, honour `Retry-After`, and record the serving endpoint per
   tile in `survey.json`.
5. **Silent partial merges.** `merge` refuses to run when any expected tile file
   is missing or zero length, unless `--force` is passed, in which case
   `survey.json` records `complete: false`.
6. **Non-atomic writes.** The DEM download already writes to `.download` and
   renames. The merge outputs do not. All writes become write-then-rename.
7. **No version control.** `git init`, a real `.gitignore`, and a first commit
   before anything is pushed.

### .gitignore

Must exclude, at minimum: `.venv/`, `__pycache__/`, `data/`, `south-wales-package-*/`,
`temp_*.osm`, `temp_*.geojson`, `*.osm`, `*.osm.pbf`, `*.parquet`, `*.tif`,
`*.egrid`, `*.blocks`, `bin/`, `obj/`. The working folder currently holds
several hundred megabytes of downloaded output that must not reach GitHub.

## Testing

Unit tests, offline and fast, covering the pure logic:

- tiling maths: tile counts, core and query bbox construction, overlap clamping
  at the study area edges
- bbox parsing and normalisation, including reversed and out-of-range input
- slugification, including non-ASCII, punctuation-only and over-length input
- folder and stem construction, and collision suffixing
- the path length guard, at and either side of the 240 character threshold
- OSM XML merge dedupe by `type/id`, and GeoJSON merge dedupe by feature `id`
- `survey.json` shape, and that `complete` is false when a tile failed

Network sources are tested against recorded fixture responses, so the suite
never makes a live call. One live smoke test on a small bbox exists but is
marked and excluded from the default run.

### Manual verification

Two things tests cannot settle, both done early:

1. **Urbano 2 accepts a renamed stem.** Build a small package with a readable
   stem, load it in Urbano 2, confirm it reads. This happens before anything is
   built on top of the naming scheme. If it fails, fall back to a readable
   folder with a coordinate stem, and only the naming section changes.
2. **The `Layers` array.** Probe whether Urbano 2 does anything with file paths
   placed in `project_setting.json`'s `Layers` array. If it does, populate it
   with the `layers/` GeoJSON files. If not, they remain standalone files
   referenced directly in Grasshopper. Either outcome is acceptable; this is
   upside only.

## Risks

| Risk | Mitigation |
| --- | --- |
| Urbano 2 rejects a renamed stem | Verified first, before dependent work; `--coordinate-stem` fallback retained |
| Overpass rate limits or is slow on large areas | Rate limiting, resume, endpoint fallback already present, honest duration estimates in the UI |
| Windows path length on deep OneDrive roots | Plan-time guard that refuses before downloading |
| Refactor regresses working behaviour | Move rather than rewrite; existing CLI subcommands retained and tested; the South Wales package acts as a reference output |

## Phase 2 handoff

Phase 2 inherits the `LayerSource` protocol, the `layers/` output folder, and
licence recording in `survey.json`. Each new source becomes one module plus one
registry entry.

Research already completed and carried forward:

- **NRW LiDAR via DataMapWales.** DTM and DSM at 25 cm, 50 cm, 1 m and 2 m over
  roughly 70% of Wales, Open Government Licence, free for commercial use, WMS
  and tile downloads available. This is the single largest accuracy gain
  available, against the 30 m COP30 currently used, and yields canopy and
  building heights from DSM minus DTM.
- **OS NGD via OS Data Hub.** Authoritative UK buildings, land use, water
  network and trees. Requires an API key and appropriate licensing.
- **Drainage and sewers.** Dŵr Cymru does not sell a bulk GIS sewer dataset.
  Records are viewable in their viewing rooms without photography or printing,
  or purchasable per property. Natural Resources Wales holds the full sewer
  network GIS but licensed for internal use only. What is openly available is
  sewer catchment boundaries, storm overflow points and treatment works
  locations. The layer should be designed around those, with a documented slot
  for a licensed dataset should one ever be obtained.
