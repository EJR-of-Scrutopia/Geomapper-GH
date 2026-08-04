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

### Which elevation model

The DEM is a choice, in Settings or with `--demtype`, and it defaults to
COP30 exactly as it always was. Be clear about what the choice is for,
because it is not what it sounds like:

**Nothing in the list is higher resolution than COP30.** OpenTopography's
global DEM API serves 30 m at best, anywhere, Wales included, and a paid
OpenTopography plan (OT+) buys high resolution LiDAR for North America
rather than sharper data anywhere else. If you came here looking for a
finer DEM for a Welsh site, it is not in this setting; it is NRW LiDAR
through DataMapWales, which is phase 2 work (see the roadmap).

What the choice actually changes is the kind of model:

| Model | Resolution | What it measures |
| --- | --- | --- |
| `COP30` (default) | 30 m | Surface: roofs and tree canopy are in the heights |
| `EU_DTM` | 30 m | Bare earth terrain, Europe including the UK |
| `AW3D30` | 30 m | Surface (JAXA ALOS) |
| `NASADEM` | 30 m | Surface (reprocessed SRTM) |
| `SRTMGL1` | 30 m | Surface (SRTM) |
| `COP90` | 90 m | Surface, coarser and quicker |
| `SRTMGL3` | 90 m | Surface, coarser |

For a site section or a ground plane, `EU_DTM` is often the more useful of
these, because a 30 m surface model puts trees and roofs into the terrain.
That, and not sharpness, is the reason this setting exists.

Every other `demtype` OpenTopography's API accepts is still valid on
`--demtype` (the ellipsoidal variants `SRTMGL1_E` and `AW3D30_E`, the
bathymetry grids, `GEDTM30`, and the Canada and South America products),
and none of them is offered in the interface: the ellipsoidal ones differ
from their ordinary siblings by tens of metres vertically with nothing on
screen to say so, the bathymetry grids and `GEDI_L3` are 500 m and 1 km,
and the regional ones do not cover Wales. `survey.json` records the model
each package actually holds, along with that model's own licence and
citation rather than Copernicus' regardless of what was downloaded.

An unrecognised model is refused before anything is downloaded, by the
same construction-time check an unrecognised `--category` goes through, so
the browser and the command line both get "Unknown elevation model" and
the list of valid ones rather than a download that fails halfway.

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
- Where the download lands sits in the main form, directly above the
  Download button: a text field holding the folder everything is saved
  under, and a Browse button beneath it. Above both, once a region and
  site are filled in, is the exact folder this run will create, dated and
  named, composed by the same code that creates it. The field holds the
  root; the line above holds the whole path. There is one such field on
  the page and not a copy of one, so the path you type there is the path
  the download uses.
- Browse opens a real Windows folder dialog and writes the chosen path
  into the field. A browser cannot hand a page a filesystem path, so the
  dialog is opened by the local server, in a short-lived child process;
  the text field is unchanged and still does the whole job on its own.
  Cancelling, a dialog left open for two minutes, and a machine with no
  picker at all each leave the field exactly as it was and say so
  underneath it. If the dialog does not appear, look behind the browser
  window: it is a native window and can open behind a maximised one.
- Tile size and overlap live behind the Settings button: defaults you set
  once, not per-survey choices. An API key field appears there per data
  source that needs one (OpenTopography's, for the elevation layer),
  driven by the same source registry the layer list comes from, so a
  second or third keyed source in a later phase needs no new panel. The
  elevation model select is built from that same registry (see "Which
  elevation model"), and it says on the page that nothing in it is
  sharper than the default. An Appearance setting there too: Match system
  (the default), Light or Dark, applied to the whole page and to the tile
  grid below.
- Tile size is a slider, and it says what each size costs for the extent
  currently drawn: the tile count and the estimated time, re-estimated
  from the server a moment after you stop moving it rather than on every
  step of a drag. It does not show a failure risk, and that is a refusal
  rather than an omission. Risk would depend on how dense the
  OpenStreetMap data is on that particular ground, which nothing here can
  know before downloading it, and a percentage or a traffic light would
  look measured while being invented. What it shows instead is the real
  trade, in a sentence that changes with the slider: larger tiles mean
  fewer requests and are quicker on sparse ground, but more of them need
  splitting on dense ground, and each split costs the requests its pieces
  take. Splitting is not a failure (see the tile grid below), so there is
  nothing else honest to warn about. The slider keeps the 500 m minimum
  the number field before it had, and a saved size outside its range
  widens the slider rather than being quietly rewritten to fit.
- Above the Download button you get the extent in kilometres, tile count,
  and an estimated download size and duration, computed before anything is
  fetched, so a mis-drawn box over the wrong country is obvious immediately.
  The same request also draws the actual tiling as rectangles on the map,
  from the server's own tile geometry rather than a client-side guess, so
  what you see is the grid the pipeline is really about to use.
- The download runs with a live per-tile log, and the tile grid shades each
  rectangle as it goes: not started, in progress, done, or failed, the last
  one drawn distinctly since it is the one worth noticing before deciding
  you have enough.
- A red tile says why it is red. Hover one, or tap it on a touchscreen, and
  the reason the run actually recorded appears on the tile: the service
  timed out, or answered 429, or answered 503 four times over, and whether
  it was retried and failed again. The same reasons are listed under the
  progress bar as well, so the account is there without having to know to
  hover anything, and clicking a red tile marks its entry in that list,
  which is how you tell one rectangle from seventy-one others. The
  sentences are the ones `survey.json` records under `tile_failures` and
  the ones the command line prints, composed in one place, so the page, the
  file and the terminal cannot tell you three different stories about the
  same run. A tile that failed and then landed on the retry is not a
  failure and stops being red; a tile the verify pass finds on disk after
  all is corrected the same way. A tile that downloaded successfully and
  happened to contain nothing is a success, is not red, and is offered no
  explanation, because it does not need one.
- A progress bar underneath answers the other question,
  how much longer. It is derived from the same progress events the grid
  is, weighted by what each layer's own estimate says it costs, so a
  finished OpenStreetMap pass reads as the large majority of the run it
  actually is. The countdown starts from the estimate and switches to the
  run's own measured rate once there is enough finished work to measure,
  and it says which of the two it is using. Nothing is reported to the
  second, because nothing here is accurate to the second. A run that
  passes its estimate says so rather than sitting at zero, a tile that
  needed splitting is called out as having made the run longer, and a
  stopped run settles at the percentage it actually reached rather than
  jumping to complete. A Stop button ends the download: it does not discard
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

# Runs the Urbano bridge, and only the bridge, over a package you already have.
mapgen bridge "C:\Surveys\South-Wales\2026-08-01_Barry-Waterfront"

# Lists every registered data source and its licence.
mapgen sources

# Lists every --category id, including the nine road subtypes.
mapgen categories
```

`mapgen bridge` is the inverse of `--skip-bridge`, and it exists for two
situations that are ordinary rather than exotic. If Urbano was not installed
when you downloaded a package, the bridge failed and that package has no
`<stem>_project_setting.json`; install Urbano, point this at the folder, and
it gets one without a byte being downloaded again. And a Stop pressed right
at the end of a run skips the bridge deliberately (see `stopped` in the
survey.json table), leaving a package that is otherwise complete, in a folder
a later survey will never reuse; this is how those files are recovered.

It reads the package's own `survey.json` for the extent, the file stem and
which layers the package holds, and re-derives none of that from the folder
name. It refuses, in one plain line, if the folder is not there, if there is
no `survey.json` in it (that package needs the survey running again, which
resumes from whatever is already on disk, not this), if the `survey.json`
cannot be read, or if a merged file the bridge needs is missing from the
package root, naming the file. One exception, for the case you will actually
meet: a package whose `<stem>.tif` is missing **and** whose `tiles` record
every elevation tile as `failed` is bridged without elevation, exactly as the
download itself would have. That is the package positively stating the layer
was attempted and did not arrive, which is different from a file having been
moved or deleted, or the wrong folder being passed, all of which are still
refused. Elevation is the only layer with an API key, so it is the only one
that fails this way routinely. Nothing but the `bridge` block of
`survey.json` is written: `complete` and `stopped` describe the download,
which this command was not present for. It exits 0 on a bridge that
succeeded and 1 on one that ran and failed, unlike `mapgen survey`, which
exits 0 for the same bridge failure because it still delivered its data.

`mapgen survey` exits 0 on a complete package and 1 if any tile failed
(`--force` continues past a failed tile instead of stopping, and marks the
package incomplete in `survey.json` rather than pretending it finished).
Either way it prints which tiles it could not get, for which layer, and why,
and every run now attempts every tile: a tile that fails no longer ends its
layer, and one whose failure a retry could plausibly fix (a timeout, a rate
limit, a 5xx) is tried once more later in the same run, after the rest of the
extent has been fetched. Without `--force` a run that is still short when
that is done exits 1 with the same account, and `survey.json` is written
before it does, so the folder explains itself.
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
  file the Urbano bridge reads for OSM geometry. **It is absent when the
  extent genuinely contained nothing**, over open water for instance, or
  under a category selection nothing on that ground matches: mapgen writes
  no empty file to stand in for data that was never there, and `survey.json`
  says so through an empty `merged_files` and a `features_merged` of zero.
  A tile that came back empty is still an `ok` tile; empty and failed are
  different things throughout.
- **`<stem>_<type>.geojson`**: one GeoJSON FeatureCollection per Overture
  type the completed request asked for, each fetched in a single request
  over the whole extent, so there are no tile seams in it to deduplicate.
  A type that returned no features over this extent gets no file, for the
  same reason `<stem>.osm` can be absent.
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
- **`<stem>.tif`**: the whole-area DEM GeoTIFF, if an elevation source was
  selected and an OpenTopography key was available. Which model it is comes
  from the `--demtype` setting (COP30 by default) and is recorded in
  `survey.json`.
- **`<stem>_project_setting.json`**: written by the Urbano bridge, only when
  the bridge succeeds. This is the single file to point Urbano 2 at. If it is
  not there, `survey.json`'s `bridge` block says why, and `mapgen bridge` on
  this folder is how to produce it later without downloading anything again.
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
| `sources` | One entry per requested source: `id`, `licence`, `attribution`, `merged_files` (the files this layer actually left in the package root, empty if it left none), `features_merged` (how many features or OSM elements went into them, absent for elevation, whose output is a raster rather than a feature collection), `endpoints_used` (which real URLs were actually contacted this run; empty if every tile was already on disk from an earlier run, since a skipped tile has no endpoint to record), for Overture `types` (the actual Overture types fetched), and for elevation `demtype` (the DEM model the package actually holds, with `licence` and `attribution` beside it being that model's own). For OpenStreetMap, `routing_note` names which endpoint (map API or Overpass) this run used and why, present only when there is something to say (absent for the default map API run, since that is not a deviation worth flagging). |
| `tiles` | One entry per tile, `tile_id` plus an `"ok"`/`"failed"`/`"pending"` status per source. Why a tile is `"failed"` is in `tile_failures` below, not here, so this row keeps the shape every earlier package already has. `"pending"` means the source never got a turn on that tile at all, most often because a Stop request landed first; it is a different, more honest claim than `"failed"`, which means a real attempt came up short. |
| `tile_failures` | One entry per tile that is still missing when the run ends: `source`, `tile_id`, `kind`, `reason` and `retried`. Empty on an ordinary run. `reason` is a plain sentence written for a person, never a traceback and never a URL. `kind` is the fixed vocabulary the retry decision is made on: `timeout`, `unreachable`, `rate_limited` and `service_error` are retried once, automatically, later in the same run; `not_authorised`, `refused`, `node_cap`, `no_output` and `unknown` are not, because asking again gets the same answer. `retried` says whether that second attempt happened. **A layer that found nothing is not in here.** An extent with no buildings in it produces no file and no failure; the empty `merged_files` in its `sources` entry is what explains that. |
| `verified` | What the end-of-run check found: `checked`, `ok`, `failed`, `pending`, and `corrections`. Every planned tile of every fetched layer is reconciled against what is genuinely on disk before the run is declared finished. `corrections` is normally empty, and when it is not it is the important part: it lists tiles this run had recorded as downloaded that had no file behind them, and which have been put right rather than left to be believed. |
| `complete` | `true` only if every requested source downloaded and merged every tile successfully. Says nothing about the Urbano bridge, which is a separate concern, recorded next. |
| `stopped` | `true` only when a Stop request is the reason `complete` is `false`, never for an ordinary tile failure and never alongside `complete: true`. Distinguishes the two ways a package can be short: `complete: false, stopped: true` is exactly as far as you asked it to go and is safe to hand to Grasshopper as is; `complete: false, stopped: false` means something failed. Re-running the same extent resumes either way. A Stop that lands after every tile has already finished leaves `complete: true, stopped: false`: nothing about the data is short, so this field has nothing to report and the run is reported as done. The only trace such a run leaves is `bridge.attempted: false`, below, and `mapgen bridge` is how you get those files without downloading the extent again. |
| `bridge` | `attempted`, `ok` and `error`: whether the Urbano bridge ran, whether it succeeded, and a plain sentence if not. `ok` is `null` if the bridge step was skipped entirely, which any run you stopped does, on purpose: `attempted` is `false` and `ok` is `null` the same as `--skip-bridge`, since starting another external process after a Stop request works against stopping promptly. That holds even when the Stop landed too late to cost you any data, so a `complete: true` package with `bridge.attempted: false` and no `--skip-bridge` is a run you stopped right at the end. Two more fields appear only once `mapgen bridge` has been run over the package afterwards, and are described in the row below. |
| `bridge.ran_at`, `bridge.during_download` | Present only after `mapgen bridge <package-dir>` (see "Command line"), which runs the bridge step alone against a package that already exists. `ran_at` is when that later attempt happened, and `attempted`, `ok` and `error` beside it describe **that** attempt rather than the download: the freshest answer to "does this package have Urbano files" is the useful one, and it is where a reader already looks. `during_download` keeps the download's own `attempted`/`ok`/`error` exactly as it wrote them, so a later success never makes the file claim the bridge succeeded during a run where it did not. Running the command a second time updates the first three again and leaves `during_download` alone: it is the original, not the previous. It is `null`, rather than a fabricated `false`, in the one case where the package's record held no readable `bridge` block for it to keep. **No `ran_at` means the block describes the download**, which is every package written before this existed and every ordinary run since. |
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
fail. Nothing about that is permanent: `mapgen bridge <package-dir>` runs the
bridge step alone against a package you already have, so a package downloaded
before Urbano was installed gets its project setting later without being
downloaded again. To build the bridge once Urbano 2 is installed:

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
