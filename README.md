# mapgen

A site survey data tool for architectural work. Draw an extent on a map, name
it, and get back a folder of OpenStreetMap, Overture Maps and elevation data
that Grasshopper and Urbano 2 can read directly.

## What it does

You give it a bounding box, a region name and a site name. It splits the
extent into overlapping tiles and downloads OpenStreetMap tile by tile,
deduplicating the overlap. Overture Maps, and the elevation model if you
have an OpenTopography key, are each fetched in one request over the whole
extent instead. Everything is merged into single per-source files and
written into a dated, named package folder alongside a `survey.json` audit
trail and an Urbano 2 project setting file. A failed or interrupted job
resumes rather than restarting.

Only OpenStreetMap is tiled, and only because the OSM map API has a hard
50,000-node cap per request that tiling exists to stay under. Overture is a
bbox-filtered read of cloud-hosted parquet with no equivalent cap, so
tiling it cost one process launch per tile per type and bought nothing: on
one measured extent, 16 tiled calls took 69 to 93 seconds against 4.5
seconds for the single call that returned the identical 9,910 features.

Overture's eight data types are downloaded at the same time rather than one
after another, which on a 10 by 14 km extent takes the same 168 MB from
around 60 seconds to around 18. OpenStreetMap is deliberately left
sequential: its tiles go to a shared public API with its own rate limits,
and pointing many concurrent requests at it is a good way to get throttled
part way through a survey.

It replaces an earlier script, `osm_overture_tiles.py`, that did the same job
by hand-composed command line. Everything that script did is either here
under a real command, or is called out below as intentionally dropped.

## Setup

Requires Python 3.11 or newer.

```powershell
git clone https://github.com/EJR-of-Scrutopia/Geomapper-GH.git mapgen
cd mapgen
.\bootstrap.ps1
.\.venv\Scripts\Activate.ps1
```

`bootstrap.ps1` creates `.venv` and installs mapgen into it in editable mode,
along with its two pinned dependencies, `requests` and `overturemaps`.
`overturemaps` is a normal Python dependency, not a separate install: pip
puts its `overturemaps` command on `.venv\Scripts` alongside `mapgen` itself,
and mapgen's Overture source calls it as a subprocess. There is nothing
further to install for Overture to work.

The Urbano 2 bridge is a separate, optional piece (see "The Urbano bridge"
below). It needs the .NET SDK to build and a working Urbano 2 install to run
against, and its absence does not stop the rest of mapgen from working.

### The OpenTopography key

The elevation layer (Copernicus DEM, via OpenTopography) needs a free API key
from [portal.opentopography.org](https://portal.opentopography.org). Without
one, every other layer still works; elevation is simply left out, and the web
interface tells you why if you try to select it.

Set it as an environment variable:

```powershell
$env:OPENTOPOGRAPHY_API_KEY = "your-key"
```

or paste it into the OpenTopography field under the Settings button in the
web interface, which saves it to `~/.mapgen/config.json`. The environment
variable always wins if both are set. `OPENTOPO_API_KEY` is accepted as an
older alias for the same variable.

The field itself is `type="password"`, so a saved key and an unsaved one
both show as the same row of dots; a small "Key saved" or "No key saved"
label next to it says which is actually true, and updates the moment you
save a new key or clear the field. It never shows the key itself, only
whether one is on disk.

## Running it

### Browser

```powershell
mapgen ui
```

(or `python -m mapgen ui` if the console script is not yet on `PATH`). This
opens a local page, served on `127.0.0.1` with a per-launch access token in
the URL, so nothing else on the machine can drive it. On the page:

- Draw a rectangle on the map, paste a `west,south,east,north` bbox string, or
  search a place name and pick a result. Whichever way you set it, the extent
  always shows as a rectangle.
- Region and Site fill in on their own: picking a place from the search
  results uses that result's own name, and drawing or pasting a rectangle
  reverse-geocodes its centre. Both stay ordinary editable text fields, and
  neither is ever overwritten once you have typed into it; if one cannot be
  worked out, the estimate panel says which is missing rather than guessing
  from coordinates.
- Tick which layers you want (OpenStreetMap, Overture Maps, elevation) and
  which categories (buildings, roads, broken down into motorway, trunk,
  primary, secondary, residential, service, footpath, cycleway and track,
  water, vegetation and landuse, rail, boundaries, points of interest).
  Categories are ticked by default, matching the everything-selected
  behaviour before this control existed. Category selection reaches
  Overture directly. It reaches OpenStreetMap by switching that download
  to Overpass automatically, since the default OSM map API cannot filter
  by tag at all; ticking everything (the default) keeps the map API,
  today's faster, well-tested path for a whole-area pull. The estimate
  panel and `survey.json` both say which of the two a run actually used
  and why, since Overpass is a separate, shared public service with its
  own rate limits (see "Limits worth knowing about"). At least one
  category is required: unticking the last one disables Download with a
  plain reason, the same way a missing site name already does, rather
  than letting you press it and get an error back.
- Output root, tile size and overlap live behind the Settings button:
  defaults you set once, not per-survey choices. An API key field appears
  there per data source that needs one (OpenTopography's, for the
  elevation layer), driven by the same source registry the layer list
  comes from, so a second or third keyed source in a later phase needs no
  new panel. An Appearance setting there too: Match system (the default),
  Light or Dark, applied to the whole page and to the tile grid below.
- Above the Download button you get the extent in kilometres, tile count,
  and an estimated download size and duration, computed before anything is
  fetched, so a mis-drawn box over the wrong country is obvious immediately.
  The same request also draws the actual tiling as rectangles on the map,
  from the server's own tile geometry rather than a client-side guess, so
  what you see is the grid the pipeline is really about to use.
- The download runs with a live per-tile log, and the tile grid shades each
  rectangle as it goes: not started, in progress, done, or failed, the last
  one drawn distinctly since it is the one worth noticing before deciding
  you have enough. A Stop button ends the download: it does not discard
  what has already been fetched. A tile already being downloaded is
  finished and kept, whatever source is running next never starts,
  everything fetched so far is merged into the package exactly as a
  finished run would, and `survey.json` records the package as stopped, not
  complete and not failed, so the folder and its own audit trail always
  agree about how far the download actually got. Re-running the same
  extent afterwards resumes rather than restarts, picking up from the
  tiles the stop left unfetched, the same as it always has for an
  interrupted or failed run. There is no separate Pause: stopping already
  keeps everything, and resuming already continues from where it stopped,
  so a second button promising the same outcome would only be one more
  thing to explain. One honest caveat about how long Stop takes to land:
  Overture's eight types download together, so a stop during that step
  waits for all eight rather than for one, and if they were already all
  running it will finish the whole Overture step before it stops. That is
  the same "an in-progress download is finished and kept" rule as
  everywhere else, and the step it applies to is now around 18 seconds
  long instead of around 60, so in practice Stop lands sooner than it did.
- A Stop server button ends the session immediately from the page itself,
  useful under a terminal launch too, not only the windowless one below.

Only one download runs at a time; starting a second while one is in progress
is refused with a clear message rather than run concurrently, because
simultaneous Overpass requests from one machine are how you get rate limited.

#### Desktop shortcut, windowless

```powershell
.\.venv\Scripts\Activate.ps1
.\assets\make_shortcut.ps1
```

Creates (or refreshes) a `mapgen.lnk` on the Desktop that launches
`pythonw.exe -m mapgen ui --windowless`: no console window, and the server
stops itself once the page is genuinely gone.

Genuinely gone is decided from three signals, not one. Closing the tab
reports itself immediately, so a real close does not wait. Returning to the
page from another window pings at once. Failing both, 90 seconds of complete
silence is taken as the page having gone away. That last number is high on
purpose: browsers throttle background tabs to roughly one timer tick a
minute, so a shorter timeout means "you looked at another window for half a
minute", not "the page is closed". A download in progress holds the server
open regardless of any of this, because killing a job someone is waiting for
is worse than leaving a process running. The Stop server button on the page
ends it immediately either way, so a crashed browser does not leave a
process behind waiting out the full grace period.
Safe to re-run any time: it overwrites the existing shortcut rather than
needing to be deleted first, which is what makes the shortcut itself safe to
regenerate instead of hand-edited.

`mapgen ui` typed into a terminal is completely unaffected by any of this:
console, output, and running until Ctrl+C regardless of the browser tab, all
exactly as before. `--windowless` is what changes the behaviour, and only the
shortcut passes it.

With no console, `mapgen ui --windowless` has nowhere to print to: Python
itself sets `sys.stdout`/`sys.stderr` to `None` under `pythonw.exe`, and an
ordinary `print()` on `None` crashes outright. Output is redirected to
`~/.mapgen/ui.log` instead, and a failure that stops the server before it can
even open the page also raises a native Windows message box pointing at that
log, so a windowless launch that cannot start is not a silent one.

### Command line

The real command used to verify this tool, kept here because it is a working
example, not an invented one:

```powershell
mapgen survey `
  --bbox=-3.29,51.38,-3.28,51.39 `
  --region "South Wales" `
  --site "Barry Waterfront" `
  --tile-size-m 1000 `
  --overlap-m 50 `
  --source osm --source overture `
  --overture-type building --overture-type water
```

`--source` is repeatable and defaults to `osm` and `overture` if omitted.
`--overture-type` is repeatable and defaults to a map-oriented set of eight
types (building, place, segment, connector, infrastructure, land_use,
land_cover, water) if omitted. `--region` and `--site` are always required.

`--category` is repeatable and defaults to every category if omitted; run
`mapgen categories` for the full list of ids, including the nine road
subtypes. An id outside that list, `building` for the real id `buildings`
being the typo actually seen in practice, is rejected outright with the
list of valid ids, not silently treated as an empty or partial selection:
a category selection that reaches neither a real filter nor an error is
worse than either. It reaches Overture's own type selection (mapped from
category to type; `--overture-type`, if also given, wins outright over
`--category` for Overture specifically, since they are two ways of
choosing the same thing). Deselecting every category that would map to
an Overture type at all, `--category rail` alone for example, correctly
fetches nothing from Overture rather than falling back to the full
default set: an empty selection and an unspecified one are treated as
two different things throughout.

Passing no `--category` at all still means every category, exactly as
above. An explicitly empty category selection, which the CLI's own
`--category` flag cannot produce (it always takes a value when given)
but the browser's checklist can, by unticking every box, is a different,
refused case: it matches nothing in either OSM's tag filter or Overture's
type mapping, and there is no real survey this tool serves where that is
a useful, deliberate answer, unlike `--category rail` above, which still
narrows to a real (if Overture-empty) filter. Refused with a plain
message that nothing is selected and at least one category is needed,
through the same `SurveyRequest` construction that an unknown id already
goes through, so a Python caller building a request directly gets the
same rejection the browser does.

For OpenStreetMap, a genuine restriction switches the download to Overpass
automatically, since the default OSM map API has no server-side filtering
at all and cannot honour one. Ticking or passing every category (the
default) keeps the map API, today's behaviour and the faster path for a
whole-area pull. This switch is automatic, not a separate flag: `mapgen
estimate` and `survey.json`'s per-source `routing_note` both say which
endpoint a run actually used and why, since Overpass is a distinct, shared
public service with its own rate limits, separate from the map API's.
`use_overpass=True` is also still available directly to Python callers as
a standing, whole-run choice independent of category selection; see
`OsmSource.configure` if calling this from Python rather than the CLI.

Other commands:

```powershell
# Same arguments as survey, but reports tiles, size and duration without downloading.
mapgen estimate --bbox=-3.29,51.38,-3.28,51.39 --region "South Wales" --site "Barry Waterfront"

# Lists every registered data source and its licence.
mapgen sources

# Lists every --category id, including the nine road subtypes.
mapgen categories
```

`mapgen survey` exits 0 on a complete package and 1 if any tile failed
(`--force` continues past a failed tile instead of stopping, and marks the
package incomplete in `survey.json` rather than pretending it finished).
`plan`, `download`, `merge` and `urbano-package` still exist as command
names, for muscle memory, but they are aliases for `survey`/`estimate` with
the current flag set, not the old script's flags: `--output-dir`, `--tile-id`
and `--max-tiles` are gone, and an old invocation using them fails with a
plain argument error rather than doing something subtly different.

## What a survey folder contains

A survey of "Barry Waterfront" in region "South Wales" produces:

```text
<output root>/South-Wales/2026-08-03_Barry-Waterfront/
  Barry-Waterfront_2026-08-03.osm                    merged OpenStreetMap XML
  Barry-Waterfront_2026-08-03_building.geojson        one file per Overture type
  Barry-Waterfront_2026-08-03_water.geojson           ever fetched into this package
  Barry-Waterfront_2026-08-03.tif                     elevation, only if selected
  Barry-Waterfront_2026-08-03_project_setting.json    point Urbano 2 here
  survey.json                                         audit trail, read this first
  layers/
    water.geojson                                     friendly-named copies of
    vegetation.geojson                                three specific Overture types,
    landuse.geojson                                   for direct use outside Urbano
```

A real run over a small extent of Barry Waterfront's docks (`osm` only)
produced 3375 nodes across 69 ways, including 19 tagged buildings, landing
correctly around 51.385 N, 3.28 W. Coordinate ranges in the merged file
usually extend a little beyond the bbox you asked for: the OSM map API
returns whole ways, not clipped fragments, so a way that crosses your
boundary comes back complete. That is correct behaviour, not a bug.

Each file:

- **`<stem>.osm`**: every OSM node, way and relation in the extent, tiles
  merged and deduplicated by id, highest version wins at a seam. This is the
  file the Urbano bridge reads for OSM geometry.
- **`<stem>_<type>.geojson`**: one GeoJSON FeatureCollection per Overture
  type the completed request asked for, each fetched in a single request
  over the whole extent, so there are no tile seams in it to deduplicate.
  A complete run also sweeps
  away any merged file an earlier, differently-scoped attempt left in the
  root, so the folder matches `survey.json` rather than accumulating types
  the current request never asked for. The sweep only ever touches names
  mapgen itself could have written for this exact survey stem: your own
  files in the folder are never candidates, whatever they are called. An
  incomplete run (`complete: false`) keeps everything it finds until a
  resume finishes the job.
- **`layers/water.geojson`, `layers/vegetation.geojson`, `layers/landuse.geojson`**:
  copies of the Overture `water`, `land_cover` and `land_use` types
  specifically, under names meant to be read straight into Grasshopper
  without knowing which Overture type produced them. Only these three are
  copied here; every other requested type is still available as the
  `<stem>_<type>.geojson` file above.
- **`<stem>.tif`**: the whole-area Copernicus DEM GeoTIFF, if an elevation
  source was selected and an OpenTopography key was available.
- **`<stem>_project_setting.json`**: written by the Urbano bridge, only when
  the bridge succeeds. This is the single file to point Urbano 2 at.
- **`survey.json`**: see below.
- **`_work/`**: the in-progress scratch folder, keyed by a fingerprint of the
  exact bbox, tile size, overlap, category selection and Overture type
  selection. It is only present while a job is incomplete, or if
  `--keep-work` was passed. A clean, complete run removes it automatically.
  Re-running the same survey with the same tiling AND the same content
  selection resumes from whatever is already in here rather than
  restarting; changing either gets a fresh, separate scratch folder of its
  own, so raw tiles fetched under an old selection are never mistaken for
  the new one's.

### survey.json

The audit trail. If this data ends up informing real project work, being able
to say where every polygon came from, under what licence, is not optional.
Fields, as actually written:

| Field | Meaning |
| --- | --- |
| `schema_version` | Currently `1`. Bump only on a breaking change to this shape. |
| `tool_version` | The mapgen version that produced this package. |
| `site`, `region` | Exactly as typed. |
| `slug.site`, `slug.region` | The versions used to build the folder path. |
| `date` | The survey date, ISO format. Defaults to today, overridable with `--date`. |
| `urbano_stem` | The file stem used for every named output in this package. |
| `bbox` | The requested extent, `west`/`south`/`east`/`north`. |
| `extent_km` | Width and height of that bbox in kilometres. |
| `tiling` | `tile_size_m` and `overlap_m` actually used, and the resulting `rows`/`cols`. Always the tiling that was requested: a tile too dense for one request is split inside its own tile (see "Limits worth knowing about") and the plan itself never changes. |
| `categories` | The resolved category selection: every id in `mapgen categories` if none was specified, otherwise exactly what was asked for. |
| `sources` | One entry per requested source: `id`, `licence`, `attribution`, `endpoints_used` (which real URLs were actually contacted this run; empty if every tile was already on disk from an earlier run, since a skipped tile has no endpoint to record), and, for Overture, `types` (the actual Overture types fetched). For OpenStreetMap, `routing_note` names which endpoint (map API or Overpass) this run used and why, present only when there is something to say (absent for the default map API run, since that is not a deviation worth flagging). |
| `tiles` | One entry per tile, `tile_id` plus an `"ok"`/`"failed"`/`"pending"` status per source. `"pending"` means the source never got a turn on that tile at all, most often because a Stop request landed first; it is a different, more honest claim than `"failed"`, which means a real attempt came up short. |
| `complete` | `true` only if every requested source downloaded and merged every tile successfully. Says nothing about the Urbano bridge, which is a separate concern, recorded next. |
| `stopped` | `true` only when a Stop request is the reason `complete` is `false`, never for an ordinary tile failure. Distinguishes the two ways a package can be short: `complete: false, stopped: true` is exactly as far as you asked it to go and is safe to hand to Grasshopper as is; `complete: false, stopped: false` means something failed. Re-running the same extent resumes either way. |
| `bridge` | `attempted`, `ok` and `error`: whether the Urbano bridge ran, whether it succeeded, and a plain sentence if not. `ok` is `null` if the bridge step was skipped entirely, which a stopped run always does, on purpose: `attempted` is `false` and `ok` is `null` the same as `--skip-bridge`, since starting another external process after a Stop request works against stopping promptly. |
| `started_at`, `finished_at` | UTC timestamps. |

## Using the output in Grasshopper, with Urbano 2

This is the point of the tool. In Grasshopper, place the Urbano 2 component
that consumes a project setting file and point its file path input at
`<stem>_project_setting.json`. That one file already carries absolute paths
to the OSM file and, if generated, the elevation GeoTIFF and an Overture
buildings geoparquet that the bridge fetches on its own account, separate
from the `.geojson` files described above.

The `layers/*.geojson` files (and the `<stem>_<type>.geojson` files in the
package root) are independent of Urbano and read directly into any
Grasshopper component that understands GeoJSON. This matters because Urbano
may or may not do anything with paths placed in its own `Layers` array; both
outcomes are fine, since these files work as standalone inputs either way.

## Data sources, licences and attribution

These are legal obligations, not decoration. `survey.json`'s `sources` array
records the exact licence and attribution string for every layer in a given
package; carry that text forward into anything published from this data,
including drawing sheets.

| Source | Licence | Attribution string |
| --- | --- | --- |
| OpenStreetMap | Open Database License (ODbL) 1.0 | (c) OpenStreetMap contributors |
| Overture Maps | Mixed by theme: Open Database License (ODbL) and CDLA-Permissive-2.0 | (c) Overture Maps Foundation |
| Copernicus DEM, via OpenTopography | Free for any use, with attribution | (c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH |

ODbL requires attribution and, if you redistribute the data itself (as
opposed to a map or drawing derived from it), requires any substantial
extract to stay under an ODbL-compatible licence. See
[osm.org/copyright](https://www.openstreetmap.org/copyright) and the
[Overture Maps licensing page](https://overturemaps.org/documentation/attribution/)
for the current, authoritative text; the table above is a working summary,
not a substitute for reading either.

## Limits worth knowing about

**The OSM node cap.** The OSM map API refuses any single request over 50000
nodes. This is the map API's own limit; Overpass has no equivalent, so it
only applies to a run that is actually using the map API, which is the
default whenever every category is selected. A tile that dense is split
into four quarters and downloaded in pieces, and a quarter that is still
too dense is split again, twice at most: a 2000 m tile becomes at most
sixteen 500 m pieces. The pieces are merged back into that tile's own
file, so the rest of the run carries on exactly as if the tile had
arrived in one request, `survey.json` records it as an ordinary `ok`
tile, and the map shows it finishing green. Each split is announced
through the progress log (`[tile_subdivided] source=osm tile_id=r03_c04
pieces=4 depth=1`).

Only the dense tile pays. The tiling itself never changes, so the work
folder, the resume state and every tile and Overture download that has
already succeeded are all untouched: a split costs at most that one
tile's own pieces, about 40 seconds at the map API's rate limit, not
another run. (Until Task 26 the whole run restarted at 1500 m and then
1000 m instead, which meant refetching everything that had already
worked; on a 72-tile extent that turned a three minute download into
fifteen.) The quarters are kept in a `_split/` folder under `_work/`
until the package completes, so a run interrupted mid-split resumes into
it rather than starting it again.

Ground still over 50000 nodes in a sixteenth of a tile fails, and mapgen
says which piece it was and how large that piece is, so "draw a smaller
extent" is a decision you make with the number in front of you. A run
already on Overpass, whether because a category filter put it there or
because `use_overpass=True` was set directly, never splits at all: there
is no node cap on that path for a smaller piece to fix.

**Overpass is a second, separate shared public service.** Once any category
is deselected, OpenStreetMap downloads switch from the map API to Overpass
to actually honour the filter (see "Category filtering" above). Overpass
has its own rate limits, its own availability, and no relation to the map
API's quota; `mapgen estimate` and `survey.json`'s per-source `routing_note`
both name which one a run depends on, so a busy Overpass instance is a known
possibility, not a mystery, when a filtered run is slower or a 429 turns up.
Endpoint rotation and rate limiting already applied to the map API apply
identically here. `use_overpass=True` remains available directly to Python
callers as a standing, whole-run choice independent of category selection.

**Windows path length.** Windows resolves a path at 260 characters by
default, and this repository's own OneDrive-synced location is already
around 110 of those before a survey folder even starts. mapgen computes the
worst-case path a job would produce, including the tiling work folder, and
refuses the job before any network call if that exceeds 240 characters,
naming the longest path it would have written and suggesting a shorter
output root or region/site name. This is a real limit, already hit once
during development, not a theoretical one: choose output roots and site
names accordingly if you are working from a deeply nested folder.

**The Urbano bridge.** Urbano 2 is optional. If it is not installed, or the
bridge fails for any other reason, mapgen records the failure as a plain
sentence in `survey.json`'s `bridge.error`, prints it to the console, and
still finishes the rest of the package: the OSM, Overture and elevation data
on disk are unaffected, only `<stem>_project_setting.json` is missing. Pass
`--skip-bridge` to skip the step outright rather than have it attempt and
fail. To build the bridge once Urbano 2 is installed:

```powershell
dotnet build tools/UrbanoBridge/UrbanoBridge.csproj
```

## Tests

Python:

```powershell
.venv\Scripts\python.exe -m pytest
```

One test, `tests/test_live_smoke.py`, hits the real OpenStreetMap API and is
excluded from the default run (`pyproject.toml` sets `addopts = "-m 'not live'"`).
Run it deliberately when you want to confirm live network access still works:

```powershell
.venv\Scripts\python.exe -m pytest -m live -v
```

The map picker's front end (`src/mapgen/web/static/app.js`) is plain
JavaScript with no build step and no bundler, so it is tested separately with
a small, dependency-free Node script rather than a JS framework:

```powershell
node tests/js/test_app.js
```

Requires Node (developed against v22). It runs the real, committed `app.js`
inside a minimal DOM and `fetch` stub and checks its behaviour directly; it
does not open a browser and cannot check map rendering or tile loading, which
still need a human looking at the page.

## Roadmap

Phase 2 adds UK survey-grade layers behind the same `LayerSource` interface
used by `osm`, `overture` and `elevation`: NRW LiDAR at up to 25 cm via
DataMapWales (Open Government Licence, and a far better elevation and
building-height source than Copernicus DEM anywhere in Wales), OS NGD
buildings and water network, and open drainage data (sewer catchments, storm
overflow points and treatment works locations; a bulk sewer network dataset
is not openly available). See `docs/superpowers/specs/` for the full design
record.
